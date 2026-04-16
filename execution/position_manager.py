"""
execution/position_manager.py — Open position lifecycle management.

Evaluates every open trade every 30 seconds:
  1. Market resolved         → close at resolution_price
  2. Trailing stop triggered → price fell trailing_stop_distance below peak
  3. Hard stop-loss          → price < entry * 0.50
  4. Lock profit near expiry → TTL < 60s AND unrealized_pnl > 0
  5. Daily loss limit hit    → close ALL positions (circuit breaker)

Additional per-cycle work:
  - Mark-to-market: update current_price, unrealized_pnl, peak_price,
    max_favorable_excursion, max_adverse_excursion on every open trade
  - Equity curve snapshot every equity_snapshot_interval_s seconds
  - Expose per-symbol exposure for the wallet dashboard
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

import aiohttp

from core import database
from core.config import AppConfig
from core.models import CloseReason, Side, Trade, TradeStatus
from engine.signal_router import SignalRouter
from execution.paper_wallet import PaperWallet

if TYPE_CHECKING:
    from execution.order_executor import PaperOrderExecutor

logger = logging.getLogger(__name__)

_CLOB_BASE = "https://clob.polymarket.com"
_HARD_STOP_LOSS = 0.50   # close if current_price < entry * 0.50


class PositionManager:
    """Monitors and closes open positions with full risk controls."""

    def __init__(
        self,
        executor: "PaperOrderExecutor",
        signal_router: SignalRouter,
        wallet: PaperWallet,
        cfg: AppConfig,
        session: Optional[aiohttp.ClientSession] = None,
        poll_interval_s: float = 30.0,
    ) -> None:
        self._executor = executor
        self._router = signal_router
        self._wallet = wallet
        self._cfg = cfg
        self._session = session
        self._own_session = session is None
        self._poll_interval = poll_interval_s
        # symbol lookup: condition_id → crypto_symbol (set when trade registered)
        self._trade_symbol: dict[str, str] = {}
        self._open_trades: dict[str, Trade] = {}   # trade_id → Trade
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_equity_snap = 0.0

    def register_trade(self, trade: Trade, symbol: str) -> None:
        self._open_trades[trade.id] = trade
        self._trade_symbol[trade.id] = symbol
        self._router.mark_position_open(trade.condition_id)

    async def start(self) -> None:
        if self._own_session:
            self._session = aiohttp.ClientSession()
        await self._reload_open_trades()
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(), name="position_manager")
        logger.info("PositionManager started — %d open positions", len(self._open_trades))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._own_session and self._session:
            await self._session.close()

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_open_trades(self) -> list[Trade]:
        return list(self._open_trades.values())

    def get_unrealized_pnl(self) -> float:
        return sum(t.unrealized_pnl for t in self._open_trades.values())

    def get_open_count(self) -> int:
        return len(self._open_trades)

    # ── Internal loop ─────────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._poll_interval)
            if not self._open_trades:
                await self._maybe_snapshot_equity()
                continue
            try:
                await self._evaluate_all()
                await self._maybe_snapshot_equity()
            except Exception as exc:
                logger.warning("PositionManager loop error: %s", exc)

    async def _evaluate_all(self) -> None:
        # Check daily loss limit once for all positions
        daily_limit_hit = self._wallet.daily_loss_breached()

        for trade_id, trade in list(self._open_trades.items()):
            try:
                market_info = await self._fetch_market(trade.condition_id)
                if market_info is None:
                    continue

                current_price = float(market_info.get("current_price", trade.entry_price))
                time_to_expiry = float(market_info.get("time_to_expiry_s", 86_400))
                resolved = bool(market_info.get("resolved", False))
                resolution_price = market_info.get("resolution_price")

                # Mark to market — update in-memory state
                self._mark_to_market(trade, current_price)

                # ── Close decisions ────────────────────────────────────────────
                close_reason: Optional[CloseReason] = None
                exit_price = current_price

                if daily_limit_hit:
                    close_reason = CloseReason.DAILY_LOSS_LIMIT
                    exit_price = current_price
                elif resolved and resolution_price is not None:
                    close_reason = CloseReason.MARKET_RESOLVED
                    exit_price = float(resolution_price)
                elif self._trailing_stop_triggered(trade, current_price):
                    close_reason = CloseReason.TRAILING_STOP
                elif current_price < trade.entry_price * _HARD_STOP_LOSS:
                    close_reason = CloseReason.STOP_LOSS
                elif time_to_expiry < 60 and trade.unrealized_pnl > 0:
                    close_reason = CloseReason.LOCK_PROFIT

                if close_reason:
                    await self._close(trade, exit_price, close_reason)

            except Exception as exc:
                logger.warning("Error evaluating trade %s: %s", trade_id, exc)

    def _mark_to_market(self, trade: Trade, current_price: float) -> None:
        """Update unrealized P&L and excursion stats for a trade."""
        trade.current_price = current_price
        contracts = trade.size_usd / trade.entry_price if trade.entry_price > 0 else 0

        if trade.side == Side.YES:
            gross = contracts * current_price
        else:
            gross = contracts * (1.0 - current_price)

        trade.unrealized_pnl = gross - trade.size_usd

        # Track peak for trailing stop
        if trade.side == Side.YES:
            favourable_price = current_price
        else:
            favourable_price = 1.0 - current_price

        if favourable_price > trade.peak_price:
            trade.peak_price = favourable_price

        # MFE / MAE
        if trade.unrealized_pnl > trade.max_favorable_excursion:
            trade.max_favorable_excursion = trade.unrealized_pnl
        if trade.unrealized_pnl < trade.max_adverse_excursion:
            trade.max_adverse_excursion = trade.unrealized_pnl

    def _trailing_stop_triggered(self, trade: Trade, current_price: float) -> bool:
        """
        Trailing stop: activate once trade is up >= trailing_stop_activation,
        then close if price pulls back >= trailing_stop_distance from peak.
        """
        activation = self._cfg.trailing_stop_activation
        distance = self._cfg.trailing_stop_distance

        contracts = trade.size_usd / trade.entry_price if trade.entry_price > 0 else 0
        if contracts == 0:
            return False

        peak_return = (trade.peak_price - trade.entry_price) / trade.entry_price
        if peak_return < activation - 1e-9:  # epsilon for float safety
            return False  # not activated yet

        if trade.side == Side.YES:
            current_return_from_peak = (trade.peak_price - current_price) / trade.peak_price
        else:
            current_return_from_peak = ((1.0 - trade.peak_price) - (1.0 - current_price)) / (1.0 - trade.peak_price) \
                if (1.0 - trade.peak_price) > 0 else 0.0

        if current_return_from_peak >= distance:
            logger.info(
                "Trailing stop triggered %s: peak=%.4f current=%.4f pullback=%.2f%%",
                trade.id, trade.peak_price, current_price, current_return_from_peak * 100,
            )
            return True
        return False

    async def _close(self, trade: Trade, exit_price: float, reason: CloseReason) -> None:
        symbol = self._trade_symbol.get(trade.id, "UNKNOWN")
        logger.info(
            "Closing %s [%s] reason=%s exit=%.4f unrealized=%+.2f MFE=%+.2f MAE=%+.2f",
            trade.id, symbol, reason.value, exit_price,
            trade.unrealized_pnl, trade.max_favorable_excursion, trade.max_adverse_excursion,
        )
        closed = await self._executor.close_trade_with_symbol(
            trade, exit_price, symbol, reason
        )
        self._open_trades.pop(trade.id, None)
        self._trade_symbol.pop(trade.id, None)
        self._router.mark_position_closed(trade.condition_id)

    async def _maybe_snapshot_equity(self) -> None:
        now = time.time()
        if now - self._last_equity_snap < self._cfg.equity_snapshot_interval_s:
            return
        self._last_equity_snap = now

        unrealized = self.get_unrealized_pnl()
        nav = self._wallet.nav(unrealized)

        closed_rows = await database.get_all_trades(limit=5_000)
        realized_pnl_cum = sum(
            float(r["pnl"]) for r in closed_rows if r["status"] == "CLOSED"
        )
        await database.insert_equity_point(
            timestamp=now,
            nav=nav,
            cash=self._wallet.cash,
            unrealized_pnl=unrealized,
            realized_pnl_cumulative=realized_pnl_cum,
            open_positions=len(self._open_trades),
        )

    # ── DB reload on restart ──────────────────────────────────────────────────

    async def _reload_open_trades(self) -> None:
        # ── Reconstruct wallet from closed trades ────────────────────────
        # On restart the wallet starts with initial_bankroll.  Subtract the
        # realised PnL of already-closed trades so cash reflects reality.
        closed_rows = await database.get_all_trades(limit=50_000)
        realized_pnl = sum(
            float(r["pnl"]) for r in closed_rows if r["status"] == "CLOSED"
        )
        if realized_pnl != 0.0:
            # Adjust cash: initial_bankroll + realized_pnl = true available
            self._wallet._cash = self._wallet._initial + realized_pnl
            self._wallet._realized_pnl = realized_pnl
            self._wallet._peak_nav = max(self._wallet._peak_nav, self._wallet._cash)
            logger.info(
                "Wallet reconstructed from DB: cash=$%.2f (realized_pnl=$%.2f)",
                self._wallet._cash, realized_pnl,
            )

        # ── Reload open positions and deduct their capital ───────────────
        rows = await database.get_open_trades()
        journal = await database.get_journal(limit=5_000) if rows else []
        for row in rows:
            trade = Trade(
                id=row["id"],
                condition_id=row["condition_id"],
                side=Side(row["side"]),
                size_usd=float(row["size_usd"]),
                entry_price=float(row["entry_price"]),
                status=TradeStatus.OPEN,
                pnl=float(row["pnl"]),
                open_ts=float(row["open_ts"]),
                slippage_usd=float(row["slippage_usd"] or 0),
                fill_fraction=float(row["fill_fraction"] or 1.0),
                current_price=float(row["entry_price"]),
                peak_price=float(row["entry_price"]),
            )
            self._open_trades[trade.id] = trade
            self._router.mark_position_open(trade.condition_id)

            # Deduct position capital so credit() on close is balanced
            symbol = "UNKNOWN"
            for j in journal:
                if j["trade_id"] == trade.id:
                    symbol = j["crypto_symbol"]
                    break
            self._trade_symbol[trade.id] = symbol
            self._wallet.deduct(symbol, trade.size_usd)
            logger.info(
                "Reloaded open trade %s (%s) size=$%.2f — wallet cash=$%.2f",
                trade.id[:8], symbol, trade.size_usd, self._wallet.cash,
            )

    async def _fetch_market(self, condition_id: str) -> Optional[dict]:
        if self._session is None:
            return None
        url = f"{_CLOB_BASE}/markets/{condition_id}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            tokens = data.get("tokens", [])
            yes_token = next(
                (t for t in tokens if t.get("outcome", "").upper() == "YES"), None
            )
            current_price = float(yes_token["price"]) if yes_token else 0.5

            from datetime import datetime, timezone
            end_date = data.get("end_date_iso", "")
            try:
                end_ts = datetime.fromisoformat(end_date.rstrip("Z")).replace(
                    tzinfo=timezone.utc
                ).timestamp()
                ttl = max(0.0, end_ts - time.time())
            except Exception:
                ttl = 86_400

            return {
                "current_price": current_price,
                "time_to_expiry_s": ttl,
                "resolved": data.get("closed") or data.get("resolved"),
                "resolution_price": data.get("resolution_price"),
            }
        except Exception as exc:
            logger.debug("Market fetch failed for %s: %s", condition_id, exc)
            return None
