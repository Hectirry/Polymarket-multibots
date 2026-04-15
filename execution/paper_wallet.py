"""
execution/paper_wallet.py — Paper trading bankroll state machine.

Tracks the actual capital available for trading.  All trade opens deduct
from cash; all closes credit back.  Provides risk controls:

  - Max concurrent open positions (hard limit)
  - Max exposure per symbol (prevents 5 correlated BTC bets)
  - Daily loss circuit breaker (halts trading for the day if hit)
  - Consecutive loss cooldown (pause after N consecutive losses)

The wallet is the single source of truth for available capital.
signal_router.update_bankroll() should be called after each change so
Kelly sizing stays accurate.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

from core.config import AppConfig
from core.models import CloseReason, Side, Trade

logger = logging.getLogger(__name__)


class InsufficientFunds(Exception):
    pass


class RiskLimitBreached(Exception):
    """Raised when a risk control blocks a new trade."""
    pass


class PaperWallet:
    """Manages paper-trading capital with full risk controls."""

    def __init__(self, initial_bankroll: float, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._initial = initial_bankroll
        self._cash = initial_bankroll
        self._realized_pnl = 0.0

        # Risk tracking
        self._open_by_symbol: dict[str, float] = defaultdict(float)  # symbol → USD exposure
        self._open_count = 0
        # Daily loss tracks only REALIZED losses (not open position capital)
        self._daily_realized_pnl_start = 0.0   # cumulative realized P&L at day open
        self._daily_loss_reset_ts = time.time()
        self._consecutive_losses = 0
        self._cooldown_until: Optional[float] = None

        # Peak NAV for drawdown calculation
        self._peak_nav = initial_bankroll

        # History of daily starting cash (reset each UTC day)
        self._last_day: int = self._today()

    # ── Queries ──────────────────────────────────────────────────────────────

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def initial_bankroll(self) -> float:
        return self._initial

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def nav(self, unrealized_pnl: float = 0.0) -> float:
        return self._cash + unrealized_pnl

    def in_cooldown(self) -> bool:
        if self._cooldown_until is None:
            return False
        if time.time() >= self._cooldown_until:
            self._cooldown_until = None
            logger.info("PaperWallet: cooldown lifted")
            return False
        remaining = self._cooldown_until - time.time()
        logger.debug("PaperWallet: cooldown active — %.0fs remaining", remaining)
        return True

    def daily_loss_breached(self) -> bool:
        """Check if realized losses today exceed the daily limit.
        Open position capital is NOT counted — only closed trade PnL."""
        self._maybe_reset_daily()
        daily_realized_loss = self._realized_pnl - self._daily_realized_pnl_start
        # daily_realized_loss is negative when we've lost money
        limit = self._initial * self._cfg.daily_loss_limit_pct
        if daily_realized_loss <= -limit:
            logger.warning(
                "PaperWallet: daily loss limit hit ($%.2f / $%.2f)",
                abs(daily_realized_loss), limit,
            )
            return True
        return False

    def symbol_exposure(self, symbol: str) -> float:
        return self._open_by_symbol.get(symbol, 0.0)

    def can_open(self, symbol: str, size_usd: float, unrealized_pnl: float = 0.0) -> tuple[bool, str]:
        """Check all risk controls. Returns (ok, reason_if_not)."""
        if self.in_cooldown():
            return False, "consecutive_loss_cooldown"
        if self.daily_loss_breached():
            return False, "daily_loss_limit"
        if self._open_count >= self._cfg.max_concurrent_positions:
            return False, f"max_concurrent_positions={self._cfg.max_concurrent_positions}"
        max_sym = self.nav(unrealized_pnl) * self._cfg.max_symbol_exposure_pct
        if self._open_by_symbol.get(symbol, 0.0) + size_usd > max_sym:
            return False, f"max_symbol_exposure {symbol} (max=${max_sym:.0f})"
        if size_usd > self._cash:
            return False, f"insufficient_cash (have=${self._cash:.2f} need=${size_usd:.2f})"
        return True, ""

    # ── Mutations ─────────────────────────────────────────────────────────────

    def deduct(self, symbol: str, size_usd: float) -> None:
        """Reserve capital for an opening trade."""
        if size_usd > self._cash:
            raise InsufficientFunds(f"Need ${size_usd:.2f}, have ${self._cash:.2f}")
        self._cash -= size_usd
        self._open_by_symbol[symbol] = self._open_by_symbol.get(symbol, 0.0) + size_usd
        self._open_count += 1
        logger.debug(
            "PaperWallet deduct $%.2f (%s) → cash=$%.2f open=%d",
            size_usd, symbol, self._cash, self._open_count,
        )

    def credit(self, symbol: str, size_usd: float, pnl: float) -> None:
        """Return capital + profit/loss when a trade closes."""
        returned = size_usd + pnl
        self._cash += max(0.0, returned)   # cash can't go below 0 from a single trade
        self._realized_pnl += pnl
        self._open_by_symbol[symbol] = max(0.0, self._open_by_symbol.get(symbol, 0.0) - size_usd)
        self._open_count = max(0, self._open_count - 1)

        # Streak tracking
        if pnl > 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._cfg.consecutive_loss_threshold:
                self._cooldown_until = time.time() + self._cfg.consecutive_loss_cooldown_s
                logger.warning(
                    "PaperWallet: %d consecutive losses → cooldown %.0fs",
                    self._consecutive_losses, self._cfg.consecutive_loss_cooldown_s,
                )

        # Track peak NAV
        current_nav = self._cash
        if current_nav > self._peak_nav:
            self._peak_nav = current_nav

        logger.debug(
            "PaperWallet credit $%.2f pnl=%+.2f (%s) → cash=$%.2f streak_loss=%d",
            size_usd, pnl, symbol, self._cash, self._consecutive_losses,
        )

    def drawdown_pct(self, unrealized_pnl: float = 0.0) -> float:
        """Current drawdown from peak NAV as a fraction."""
        current = self.nav(unrealized_pnl)
        if self._peak_nav <= 0:
            return 0.0
        return max(0.0, (self._peak_nav - current) / self._peak_nav)

    def total_return_pct(self, unrealized_pnl: float = 0.0) -> float:
        return (self.nav(unrealized_pnl) - self._initial) / self._initial if self._initial else 0.0

    def summary(self, unrealized_pnl: float = 0.0) -> dict:
        return {
            "cash": round(self._cash, 2),
            "nav": round(self.nav(unrealized_pnl), 2),
            "initial": self._initial,
            "realized_pnl": round(self._realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "open_positions": self._open_count,
            "drawdown_pct": round(self.drawdown_pct(unrealized_pnl) * 100, 2),
            "total_return_pct": round(self.total_return_pct(unrealized_pnl) * 100, 2),
            "consecutive_losses": self._consecutive_losses,
            "cooldown_until": self._cooldown_until,
            "daily_loss_limit_pct": self._cfg.daily_loss_limit_pct * 100,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _today(self) -> int:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).day

    def _maybe_reset_daily(self) -> None:
        today = self._today()
        if today != self._last_day:
            self._daily_realized_pnl_start = self._realized_pnl
            self._last_day = today
            logger.info(
                "PaperWallet: new UTC day — daily loss reset, realized_pnl=$%.2f",
                self._realized_pnl,
            )
