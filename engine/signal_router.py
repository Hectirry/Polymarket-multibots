"""
engine/signal_router.py — Multi-stage signal pipeline and trade decision gate.

Pipeline (in order, each step can veto):
  1. Liquidity filter   — volume_24h, open_interest, spread
  2. Timing filter      — time_to_expiry window
  3. Orderbook check    — is_manipulated blocks the market
  4. Probability engine — compute delta
  5. EV calculator      — ev_net_fees must clear threshold
  6. Price bounds       — 0.05 ≤ entry_price ≤ 0.95
  7. Setup quality gate — historical win-rate check (if enabled)
  8. LLM validator      — optional, only when whale_score ≥ threshold
  9. Deduplication      — no duplicate open positions

Output: Signal dataclass or None (with a reason logged at INFO level).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from core.config import AppConfig
from core.interfaces import OrderbookAnalyzerInterface
from core.models import MarketSnapshot, PriceSnapshot, Side, Signal, WhaleSignal
from engine import ev_calculator, probability, timing
from engine.llm_validator import LLMValidator

logger = logging.getLogger(__name__)


@dataclass
class RouterDecision:
    """Full verdict from the router — structured so the snapshot logger
    can persist *why* a market was rejected, not just that it was."""

    signal: Optional[Signal]
    stage: str = ""          # '' on accept, else: liquidity/timing/orderbook/...
    reason: str = ""         # human-readable detail
    delta: Optional[float] = None
    ev_net: Optional[float] = None

    @property
    def accepted(self) -> bool:
        return self.signal is not None


class SignalRouter:
    """Runs the full signal-generation pipeline for a market snapshot."""

    def __init__(
        self,
        cfg: AppConfig,
        orderbook_analyzer: OrderbookAnalyzerInterface,
        llm_validator: Optional[LLMValidator] = None,
        bankroll: float = 1_000.0,
    ) -> None:
        self._cfg = cfg
        self._ob_analyzer = orderbook_analyzer
        self._llm = llm_validator
        self._bankroll = bankroll
        self._open_positions: set[str] = set()  # condition_ids with open trades

    def mark_position_open(self, condition_id: str) -> None:
        self._open_positions.add(condition_id)

    def mark_position_closed(self, condition_id: str) -> None:
        self._open_positions.discard(condition_id)

    def update_bankroll(self, bankroll: float) -> None:
        self._bankroll = bankroll

    async def evaluate(
        self,
        market: MarketSnapshot,
        price: Optional[PriceSnapshot],
        whale: Optional[WhaleSignal] = None,
        setup_win_rate: Optional[float] = None,
        setup_sample_count: int = 0,
    ) -> Optional[Signal]:
        """Thin back-compat wrapper around evaluate_full() returning just the Signal."""
        decision = await self.evaluate_full(
            market, price, whale, setup_win_rate, setup_sample_count
        )
        return decision.signal

    async def evaluate_full(
        self,
        market: MarketSnapshot,
        price: Optional[PriceSnapshot],
        whale: Optional[WhaleSignal] = None,
        setup_win_rate: Optional[float] = None,
        setup_sample_count: int = 0,
        prob_est=None,
    ) -> RouterDecision:
        """
        Run the pipeline and return a full decision (signal + reject stage/reason).
        All rejections are logged at INFO so operators can audit decisions.
        """
        cid = market.condition_id
        sym = market.crypto_symbol

        def reject(stage: str, reason: str, delta: Optional[float] = None,
                   ev: Optional[float] = None) -> RouterDecision:
            logger.info("REJECT %s — [%s] %s", cid, stage, reason)
            return RouterDecision(signal=None, stage=stage, reason=reason,
                                  delta=delta, ev_net=ev)

        # ── 1. Liquidity filter ───────────────────────────────────────────────
        min_vol = self._cfg.get_for_symbol(sym, "min_volume_24h")
        if market.volume_24h < min_vol:
            return reject("liquidity", f"volume_24h {market.volume_24h:.0f} < {min_vol:.0f}")
        if market.open_interest < self._cfg.min_open_interest:
            return reject("liquidity", f"open_interest {market.open_interest:.0f} < {self._cfg.min_open_interest:.0f}")
        if market.spread > self._cfg.max_spread:
            return reject("liquidity", f"spread {market.spread:.4f} > {self._cfg.max_spread:.4f}")

        # ── 2. Timing filter ─────────────────────────────────────────────────
        min_ttl = self._cfg.get_for_symbol(sym, "min_time_remaining_s")
        max_ttl = self._cfg.get_for_symbol(sym, "max_time_remaining_s")
        if not (min_ttl <= market.time_to_expiry_s <= max_ttl):
            return reject("timing",
                          f"TTL {market.time_to_expiry_s:.0f}s out of [{min_ttl:.0f}, {max_ttl:.0f}]")

        # ── 3. Orderbook manipulation check ──────────────────────────────────
        ob = await self._ob_analyzer.analyze(market.token_id)
        if ob.is_manipulated:
            return reject(
                "orderbook",
                f"manipulated top3_conc={ob.top3_concentration:.2f} > {self._cfg.orderbook_manipulation_threshold:.2f}",
            )

        # ── 4. Probability engine ─────────────────────────────────────────────
        if prob_est is None:
            prob_est = probability.estimate(market, price, whale)
        min_delta = self._cfg.get_for_symbol(sym, "min_delta")
        adj_delta = prob_est.adjusted_delta

        if adj_delta >= min_delta:
            side = Side.YES
            entry_price = market.best_ask
        elif adj_delta <= -min_delta:
            side = Side.NO
            entry_price = 1.0 - market.best_bid
        else:
            return reject(
                "probability",
                f"|delta|={abs(adj_delta):.4f} < {min_delta:.4f} "
                f"(binance_p={prob_est.binance_implied_prob:.3f} market_p={prob_est.market_implied_prob:.3f})",
                delta=adj_delta,
            )

        # ── 5. EV filter ──────────────────────────────────────────────────────
        ev_result = ev_calculator.calculate_ev(prob_est.our_prob, entry_price)
        min_ev = self._cfg.get_for_symbol(sym, "min_ev_threshold")
        if ev_result.ev_net_fees < min_ev:
            return reject("ev", f"ev_net={ev_result.ev_net_fees:.4f} < {min_ev:.4f}",
                          delta=adj_delta, ev=ev_result.ev_net_fees)

        # ── 6. Price bounds ───────────────────────────────────────────────────
        if not (self._cfg.min_contract_price <= entry_price <= self._cfg.max_contract_price):
            return reject(
                "price_bounds",
                f"entry_price={entry_price:.4f} out of [{self._cfg.min_contract_price:.2f}, {self._cfg.max_contract_price:.2f}]",
                delta=adj_delta, ev=ev_result.ev_net_fees,
            )

        # ── 7. Setup quality gate ─────────────────────────────────────────────
        if self._cfg.setup_quality_gate_enabled and setup_sample_count >= self._cfg.setup_quality_min_sample:
            if setup_win_rate is not None and setup_win_rate < self._cfg.setup_quality_min_wr:
                return reject(
                    "setup_quality",
                    f"historical WR={setup_win_rate:.2f} < {self._cfg.setup_quality_min_wr:.2f} (n={setup_sample_count})",
                    delta=adj_delta, ev=ev_result.ev_net_fees,
                )

        # ── 8. LLM validator (optional) ───────────────────────────────────────
        whale_score = whale.pressure_score if whale else 0.0
        whale_count = whale.trade_count if whale else 0
        whale_vol = whale.total_volume_usd if whale else 0.0
        whale_dir = whale.direction.value if whale else "NONE"
        spot_price = price.price if price else 0.0

        llm_validated = True
        llm_reason = ""
        if self._llm and whale_score >= self._cfg.llm_min_whale_score:
            llm_result = await self._llm.validate(
                condition_id=cid,
                question=market.question,
                crypto_symbol=sym,
                spot_price=spot_price,
                market_implied_prob=prob_est.market_implied_prob,
                our_estimated_prob=prob_est.our_prob,
                delta=adj_delta,
                ev_net_fees=ev_result.ev_net_fees,
                whale_trade_count=whale_count,
                whale_volume_usd=whale_vol,
                whale_direction=whale_dir,
                time_remaining_s=market.time_to_expiry_s,
                depth_score=ob.depth_score,
                whale_pressure_score=whale_score,
            )
            llm_validated = llm_result.validated
            llm_reason = llm_result.reason
            if not llm_validated:
                return reject("llm", f"LLM rejected: {llm_reason}",
                              delta=adj_delta, ev=ev_result.ev_net_fees)

        # ── 9. Deduplication ──────────────────────────────────────────────────
        if cid in self._open_positions:
            return reject("dedup", "position already open",
                          delta=adj_delta, ev=ev_result.ev_net_fees)

        # ── Timing adjustments ────────────────────────────────────────────────
        timing_adj = timing.compute_timing_adjustment(market, adj_delta)
        if not timing_adj.allow_entry:
            return reject("timing_adj", timing_adj.reason,
                          delta=adj_delta, ev=ev_result.ev_net_fees)

        # ── Size calculation ──────────────────────────────────────────────────
        size_usd = ev_calculator.kelly_size(
            our_prob=prob_est.our_prob,
            entry_price=entry_price,
            bankroll=self._bankroll,
            kelly_fraction=self._cfg.kelly_fraction,
            max_position_pct=self._cfg.max_position_pct,
        )
        size_usd *= timing_adj.size_multiplier

        if size_usd < 1.0:
            return reject("size", f"size too small (${size_usd:.2f})",
                          delta=adj_delta, ev=ev_result.ev_net_fees)

        logger.info(
            "SIGNAL %s %s: entry=%.4f size=$%.2f delta=%+.4f ev=%.4f "
            "depth=%.2f llm=%s",
            sym, cid, entry_price, size_usd, adj_delta,
            ev_result.ev_net_fees, ob.depth_score, llm_validated,
        )
        signal = Signal(
            condition_id=cid,
            crypto_symbol=sym,
            question=market.question,
            side=side,
            entry_price=entry_price,
            size_usd=size_usd,
            delta=adj_delta,
            ev_net_fees=ev_result.ev_net_fees,
            our_prob=prob_est.our_prob,
            market_prob=prob_est.market_implied_prob,
            whale_score=whale_score,
            llm_validated=llm_validated,
            llm_reason=llm_reason,
            quality_score=timing_adj.confidence_multiplier,
            depth_score=ob.depth_score,
            effective_spread=ob.effective_spread,
            timestamp=time.time(),
        )
        return RouterDecision(signal=signal, stage="", reason="accepted",
                              delta=adj_delta, ev_net=ev_result.ev_net_fees)
