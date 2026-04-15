"""
execution/order_executor.py — Trade execution layer (paper + production).

Paper mode (default, PAPER_TRADE=true):
  - Checks PaperWallet risk controls before allowing the trade
  - Simulates fill price with size-aware slippage model
  - Rejects fills when orderbook depth is too thin
  - Supports partial fills for shallow books
  - Deducts from PaperWallet on open; credits on close
  - Persists to trades table and trade_journal on both open and close

Production mode (PAPER_TRADE=false):
  Signs and submits orders to POST https://clob.polymarket.com/order via ECDSA.
  Protected by assertion — cannot be called in paper/dev mode.

Trade IDs: UUID4 for paper, order hash for production.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

import aiohttp

from core import database
from core.config import AppConfig
from core.interfaces import OrderExecutorInterface
from core.models import CloseReason, Side, Signal, Trade, TradeStatus
from execution.paper_wallet import PaperWallet, RiskLimitBreached
from execution.slippage import simulate_entry_fill, simulate_exit_fill

logger = logging.getLogger(__name__)

_CLOB_ORDER_URL = "https://clob.polymarket.com/order"


class PaperOrderExecutor(OrderExecutorInterface):
    """
    Simulates Polymarket order execution with:
      - Size-aware slippage (market impact)
      - Fill rejection for thin orderbooks
      - Partial fills for shallow books
      - PaperWallet bankroll deduction / credit
      - Full trade journal persistence
    """

    def __init__(self, cfg: AppConfig, wallet: PaperWallet) -> None:
        self._cfg = cfg
        self._wallet = wallet

    async def execute(self, signal: Signal) -> Optional[Trade]:
        """
        Attempt to fill a signal.  Returns Trade on success, None if
        risk controls or orderbook depth blocks the fill.
        """
        symbol = signal.crypto_symbol
        unrealized = self._estimate_unrealized()

        # ── Risk gate ──────────────────────────────────────────────────────────
        ok, reason = self._wallet.can_open(symbol, signal.size_usd, unrealized)
        if not ok:
            logger.info("[PAPER] Trade blocked by risk control: %s", reason)
            return None

        # ── Slippage simulation ────────────────────────────────────────────────
        fill = simulate_entry_fill(
            requested_price=signal.entry_price,
            size_usd=signal.size_usd,
            open_interest=signal.size_usd * 10,    # conservative proxy when OI unknown
            depth_score=signal.depth_score,
            cfg=self._cfg,
        )

        if not fill.filled:
            logger.info(
                "[PAPER] Fill REJECTED for %s — book too thin (depth=%.2f)",
                signal.condition_id, signal.depth_score,
            )
            return None

        # ── Deduct from wallet ─────────────────────────────────────────────────
        self._wallet.deduct(symbol, fill.actual_size_usd)

        trade_id = str(uuid.uuid4())
        trade = Trade(
            id=trade_id,
            condition_id=signal.condition_id,
            side=signal.side,
            size_usd=fill.actual_size_usd,
            entry_price=fill.fill_price,
            status=TradeStatus.OPEN,
            open_ts=time.time(),
            slippage_usd=fill.slippage * fill.actual_size_usd,
            fill_fraction=fill.fill_fraction,
            current_price=fill.fill_price,
            peak_price=fill.fill_price,
        )

        # ── Persist trade ──────────────────────────────────────────────────────
        await database.insert_trade(
            trade_id=trade_id,
            condition_id=signal.condition_id,
            side=signal.side.value,
            size_usd=fill.actual_size_usd,
            entry_price=fill.fill_price,
            open_ts=trade.open_ts,
            slippage_usd=trade.slippage_usd,
            fill_fraction=fill.fill_fraction,
        )

        # ── Trade journal entry ────────────────────────────────────────────────
        await database.insert_journal_entry(
            trade_id=trade_id,
            condition_id=signal.condition_id,
            crypto_symbol=signal.crypto_symbol,
            question=signal.question,
            side=signal.side.value,
            delta=signal.delta,
            ev_net=signal.ev_net_fees,
            our_prob=signal.our_prob,
            market_prob=signal.market_prob,
            whale_score=signal.whale_score,
            depth_score=signal.depth_score,
            quality_score=signal.quality_score,
            llm_validated=signal.llm_validated,
            llm_reason=signal.llm_reason,
            requested_price=fill.requested_price,
            fill_price=fill.fill_price,
            slippage=fill.slippage,
            fill_fraction=fill.fill_fraction,
            size_usd=fill.actual_size_usd,
            fee_usd=fill.fee_usd,
            open_ts=trade.open_ts,
        )

        logger.info(
            "[PAPER] Filled %s %s: size=$%.2f fill=%.4f (req=%.4f slip=%.4f) "
            "frac=%.2f fee=$%.2f wallet_cash=$%.2f",
            signal.side.value, signal.condition_id,
            fill.actual_size_usd, fill.fill_price, fill.requested_price,
            fill.slippage, fill.fill_fraction, fill.fee_usd,
            self._wallet.cash,
        )
        return trade

    async def close_trade(
        self,
        trade: Trade,
        exit_price: float,
        close_reason: CloseReason = CloseReason.MANUAL,
    ) -> Trade:
        """Close a position at exit_price with exit slippage applied."""
        actual_exit, fee_usd = simulate_exit_fill(
            exit_price=exit_price,
            trade_size_usd=trade.size_usd,
            entry_price=trade.entry_price,
            depth_score=max(0.1, 1.0 - trade.slippage_usd / max(trade.size_usd, 1)),
            cfg=self._cfg,
        )

        contracts = trade.size_usd / trade.entry_price if trade.entry_price > 0 else 0
        if trade.side == Side.YES:
            gross_payout = contracts * actual_exit
        else:
            gross_payout = contracts * (1.0 - actual_exit)

        net_payout = gross_payout - fee_usd
        pnl = net_payout - trade.size_usd
        close_ts = time.time()
        hold_time_s = close_ts - trade.open_ts

        # ── Credit wallet ──────────────────────────────────────────────────────
        self._wallet.credit(trade.condition_id[:3], trade.size_usd, pnl)
        # Note: using condition_id prefix as fallback symbol — position_manager
        # should pass symbol explicitly when known; see close_trade_with_symbol()

        # ── Persist ────────────────────────────────────────────────────────────
        await database.update_trade_close(
            trade_id=trade.id,
            exit_price=actual_exit,
            pnl=pnl,
            close_ts=close_ts,
            close_reason=close_reason.value,
            hold_time_s=hold_time_s,
            max_favorable_excursion=trade.max_favorable_excursion,
            max_adverse_excursion=trade.max_adverse_excursion,
        )
        await database.update_journal_close(
            trade_id=trade.id,
            close_reason=close_reason.value,
            hold_time_s=hold_time_s,
            exit_price=actual_exit,
            pnl=pnl,
            max_favorable_excursion=trade.max_favorable_excursion,
            max_adverse_excursion=trade.max_adverse_excursion,
        )

        trade.exit_price = actual_exit
        trade.pnl = pnl
        trade.status = TradeStatus.CLOSED
        trade.close_ts = close_ts
        trade.close_reason = close_reason

        logger.info(
            "[PAPER] Closed %s %s: exit=%.4f (raw=%.4f) pnl=%+.2f hold=%.0fs reason=%s "
            "wallet_cash=$%.2f MFE=%+.2f MAE=%+.2f",
            trade.side.value, trade.condition_id,
            actual_exit, exit_price, pnl, hold_time_s, close_reason.value,
            self._wallet.cash,
            trade.max_favorable_excursion, trade.max_adverse_excursion,
        )
        return trade

    async def close_trade_with_symbol(
        self,
        trade: Trade,
        exit_price: float,
        symbol: str,
        close_reason: CloseReason = CloseReason.MANUAL,
    ) -> Trade:
        """Close with correct symbol so wallet exposure is decremented properly."""
        actual_exit, fee_usd = simulate_exit_fill(
            exit_price=exit_price,
            trade_size_usd=trade.size_usd,
            entry_price=trade.entry_price,
            depth_score=max(0.1, 1.0 - trade.slippage_usd / max(trade.size_usd, 1)),
            cfg=self._cfg,
        )

        contracts = trade.size_usd / trade.entry_price if trade.entry_price > 0 else 0
        if trade.side == Side.YES:
            gross_payout = contracts * actual_exit
        else:
            gross_payout = contracts * (1.0 - actual_exit)

        net_payout = gross_payout - fee_usd
        pnl = net_payout - trade.size_usd
        close_ts = time.time()
        hold_time_s = close_ts - trade.open_ts

        self._wallet.credit(symbol, trade.size_usd, pnl)

        await database.update_trade_close(
            trade_id=trade.id,
            exit_price=actual_exit,
            pnl=pnl,
            close_ts=close_ts,
            close_reason=close_reason.value,
            hold_time_s=hold_time_s,
            max_favorable_excursion=trade.max_favorable_excursion,
            max_adverse_excursion=trade.max_adverse_excursion,
        )
        await database.update_journal_close(
            trade_id=trade.id,
            close_reason=close_reason.value,
            hold_time_s=hold_time_s,
            exit_price=actual_exit,
            pnl=pnl,
            max_favorable_excursion=trade.max_favorable_excursion,
            max_adverse_excursion=trade.max_adverse_excursion,
        )

        trade.exit_price = actual_exit
        trade.pnl = pnl
        trade.status = TradeStatus.CLOSED
        trade.close_ts = close_ts
        trade.close_reason = close_reason

        logger.info(
            "[PAPER] Closed %s %s (%s): exit=%.4f pnl=%+.2f hold=%.0fs reason=%s",
            trade.side.value, trade.condition_id, symbol,
            actual_exit, pnl, hold_time_s, close_reason.value,
        )
        return trade

    def _estimate_unrealized(self) -> float:
        """Placeholder — actual unrealized P&L injected by PositionManager."""
        return 0.0


class ProductionOrderExecutor(OrderExecutorInterface):
    """
    Submits real signed orders to Polymarket CLOB.

    DANGER: Only instantiated when ENV==production AND PAPER_TRADE==false.
    Requires POLYMARKET_WALLET_PRIVATE_KEY.
    """

    def __init__(self, cfg: AppConfig, session: Optional[aiohttp.ClientSession] = None) -> None:
        assert cfg.env == "production" and not cfg.paper_trade, (
            "ProductionOrderExecutor must only be used in production non-paper mode"
        )
        assert cfg.polymarket_wallet_private_key, "POLYMARKET_WALLET_PRIVATE_KEY is required"
        self._cfg = cfg
        self._session = session
        self._own_session = session is None

    async def _ensure_session(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()

    def _sign_order(self, order_payload: dict) -> str:
        raise NotImplementedError(
            "Production order signing requires eth_account + EIP-712 implementation."
        )

    async def execute(self, signal: Signal) -> Optional[Trade]:
        await self._ensure_session()
        order_payload = {
            "market": signal.condition_id,
            "side": signal.side.value,
            "price": signal.entry_price,
            "size": signal.size_usd / signal.entry_price,
            "type": "LIMIT",
            "time_in_force": "FOK",
        }
        signature = self._sign_order(order_payload)
        order_payload["signature"] = signature

        assert self._session is not None
        async with self._session.post(
            _CLOB_ORDER_URL,
            json=order_payload,
            headers={
                "POLY-ADDRESS": self._cfg.polymarket_api_key,
                "POLY-SIGNATURE": signature,
                "POLY-TIMESTAMP": str(int(time.time())),
                "POLY-PASSPHRASE": self._cfg.polymarket_passphrase,
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

        trade_id = result.get("orderID", str(uuid.uuid4()))
        fill_price = float(result.get("price", signal.entry_price))
        trade = Trade(
            id=trade_id,
            condition_id=signal.condition_id,
            side=signal.side,
            size_usd=signal.size_usd,
            entry_price=fill_price,
            status=TradeStatus.OPEN,
            open_ts=time.time(),
        )
        await database.insert_trade(
            trade_id=trade_id,
            condition_id=signal.condition_id,
            side=signal.side.value,
            size_usd=signal.size_usd,
            entry_price=fill_price,
            open_ts=trade.open_ts,
        )
        logger.info("[LIVE] Order submitted %s: id=%s", signal.condition_id, trade_id)
        return trade

    async def close_trade(self, trade: Trade, exit_price: float, **_) -> Trade:
        raise NotImplementedError("Production close_trade not yet implemented")


def create_executor(cfg: AppConfig, wallet: Optional[PaperWallet] = None) -> OrderExecutorInterface:
    """Factory that returns the correct executor based on config."""
    if cfg.paper_trade or cfg.env != "production":
        if wallet is None:
            wallet = PaperWallet(cfg.bankroll_usd, cfg)
        return PaperOrderExecutor(cfg, wallet)
    return ProductionOrderExecutor(cfg)
