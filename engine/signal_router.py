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
from typing import Optional

from core.config import AppConfig
from core.interfaces import OrderbookAnalyzerInterface
from core.models import MarketSnapshot, PriceSnapshot, Side, Signal, WhaleSignal
from engine import ev_calculator, probability, timing
from engine.llm_validator import LLMValidator

logger = logging.getLogger(__name__)


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
        """
        Run the pipeline. Returns a Signal on success, None on any veto.
        All rejections are logged at INFO so operators can audit decisions.
        """
        cid = market.condition_id
        sym = market.crypto_symbol

        # ── 1. Liquidity filter ───────────────────────────────────────────────
        min_vol = self._cfg.get_for_symbol(sym, "min_volume_24h")
        if market.volume_24h < min_vol:
            logger.info("REJECT %s — volume_24h %.0f < %.0f", cid, market.volume_24h, min_vol)
            return None
        if market.open_interest < self._cfg.min_open_interest:
            logger.info("REJECT %s — open_interest %.0f < %.0f", cid, market.open_interest, self._cfg.min_open_interest)
            return None
        if market.spread > self._cfg.max_spread:
            logger.info("REJECT %s — spread %.4f > %.4f", cid, market.spread, self._cfg.max_spread)
            return None

        # ── 2. Timing filter ─────────────────────────────────────────────────
        min_ttl = self._cfg.get_for_symbol(sym, "min_time_remaining_s")
        max_ttl = self._cfg.max_time_remaining_s
        if not (min_ttl <= market.time_to_expiry_s <= max_ttl):
            logger.info("REJECT %s — TTL %.0fs out of [%.0f, %.0f]",
                        cid, market.time_to_expiry_s, min_ttl, max_ttl)
            return None

        # ── 3. Orderbook manipulation check ──────────────────────────────────
        ob = await self._ob_analyzer.analyze(market.token_id)
        if ob.is_manipulated:
            logger.info(
                "REJECT %s — orderbook manipulated (top3_conc=%.2f > %.2f)",
                cid, ob.top3_concentration, self._cfg.orderbook_manipulation_threshold,
            )
            return None

        # ── 4. Probability engine ─────────────────────────────────────────────
        prob_est = probability.estimate(market, price, whale)
        min_delta = self._cfg.get_for_symbol(sym, "min_delta")
        adj_delta = prob_est.adjusted_delta

        # Determine trade direction
        if adj_delta >= min_delta:
            side = Side.YES
            entry_price = market.best_ask   # buying YES → pay ask
        elif adj_delta <= -min_delta:
            side = Side.NO
            entry_price = 1.0 - market.best_bid   # buying NO ≡ selling YES at bid
        else:
            logger.info(
                "REJECT %s — |delta|=%.4f < %.4f (binance_p=%.3f market_p=%.3f)",
                cid, abs(adj_delta), min_delta,
                prob_est.binance_implied_prob, prob_est.market_implied_prob,
            )
            return None

        # ── 5. EV filter ──────────────────────────────────────────────────────
        ev_result = ev_calculator.calculate_ev(prob_est.our_prob, entry_price)
        min_ev = self._cfg.get_for_symbol(sym, "min_ev_threshold")
        if ev_result.ev_net_fees < min_ev:
            logger.info(
                "REJECT %s — ev_net=%.4f < %.4f",
                cid, ev_result.ev_net_fees, min_ev,
            )
            return None

        # ── 6. Price bounds ───────────────────────────────────────────────────
        if not (self._cfg.min_contract_price <= entry_price <= self._cfg.max_contract_price):
            logger.info(
                "REJECT %s — entry_price=%.4f out of [%.2f, %.2f]",
                cid, entry_price, self._cfg.min_contract_price, self._cfg.max_contract_price,
            )
            return None

        # ── 7. Setup quality gate ─────────────────────────────────────────────
        if self._cfg.setup_quality_gate_enabled and setup_sample_count >= self._cfg.setup_quality_min_sample:
            if setup_win_rate is not None and setup_win_rate < self._cfg.setup_quality_min_wr:
                logger.info(
                    "REJECT %s — historical WR=%.2f < %.2f (n=%d)",
                    cid, setup_win_rate, self._cfg.setup_quality_min_wr, setup_sample_count,
                )
                return None

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
                logger.info("REJECT %s — LLM rejected: %s", cid, llm_reason)
                return None

        # ── 9. Deduplication ──────────────────────────────────────────────────
        if cid in self._open_positions:
            logger.info("REJECT %s — position already open", cid)
            return None

        # ── Timing adjustments ────────────────────────────────────────────────
        timing_adj = timing.compute_timing_adjustment(market, adj_delta)
        if not timing_adj.allow_entry:
            logger.info("REJECT %s — timing: %s", cid, timing_adj.reason)
            return None

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
            logger.info("REJECT %s — size too small ($%.2f)", cid, size_usd)
            return None

        logger.info(
            "SIGNAL %s %s: entry=%.4f size=$%.2f delta=%+.4f ev=%.4f "
            "depth=%.2f llm=%s",
            sym, cid, entry_price, size_usd, adj_delta,
            ev_result.ev_net_fees, ob.depth_score, llm_validated,
        )
        return Signal(
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
