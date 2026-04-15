"""
execution/analytics.py — Paper trading performance metrics.

Computes from closed trade history:
  - Win rate, profit factor, expectancy
  - Sharpe ratio (daily returns, annualised)
  - Maximum drawdown (from equity curve)
  - Average / median hold time
  - Consecutive win/loss streaks
  - Per-symbol breakdown
  - Close reason distribution

All computation is read-only — no DB writes.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from typing import Optional

import aiosqlite

from core import database
from core.models import PaperAnalytics

logger = logging.getLogger(__name__)


async def compute_analytics(
    wallet_nav: float,
    wallet_cash: float,
    initial_bankroll: float,
    open_count: int,
) -> PaperAnalytics:
    """Compute the full analytics snapshot from DB state."""

    closed_rows = await database.get_all_trades(limit=5_000)
    closed = [r for r in closed_rows if r["status"] == "CLOSED"]
    signal_rows = await database.get_recent_signals(limit=5_000)
    cid_to_sym = {r["condition_id"]: r["crypto_symbol"] for r in signal_rows}

    total = len(closed)
    if total == 0:
        return _empty_analytics(wallet_nav, wallet_cash, initial_bankroll, open_count)

    wins = [r for r in closed if float(r["pnl"]) > 0]
    losses = [r for r in closed if float(r["pnl"]) <= 0]
    win_rate = len(wins) / total if total else 0.0

    gross_profit = sum(float(r["pnl"]) for r in wins)
    gross_loss = sum(abs(float(r["pnl"])) for r in losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    total_pnl = gross_profit - gross_loss
    expectancy = total_pnl / total if total else 0.0

    # Fees and slippage
    total_fees = sum(float(r["size_usd"] or 0) * 0.02 for r in closed)  # approximation
    total_slippage = sum(float(r["slippage_usd"] or 0) for r in closed)

    # Hold times
    hold_times = [float(r["hold_time_s"]) for r in closed if r["hold_time_s"] is not None]
    avg_hold = statistics.mean(hold_times) if hold_times else 0.0
    med_hold = statistics.median(hold_times) if hold_times else 0.0

    # Sharpe ratio from daily P&L series
    sharpe = await _compute_sharpe()

    # Drawdown from equity curve
    max_dd_pct, max_dd_usd, curr_dd_pct = await _compute_drawdown(initial_bankroll)

    # Streak analysis
    pnl_series = await database.get_pnl_series()
    cur_streak, max_win_streak, max_loss_streak = _compute_streaks(pnl_series)

    # Per-symbol breakdown
    by_symbol = _compute_by_symbol(closed, cid_to_sym)

    # Close reasons
    closes_by_reason: dict[str, int] = defaultdict(int)
    for r in closed:
        reason = r["close_reason"] or "unknown"
        closes_by_reason[reason] += 1

    total_return_pct = (wallet_nav - initial_bankroll) / initial_bankroll * 100 if initial_bankroll else 0.0

    return PaperAnalytics(
        total_trades=total,
        open_trades=open_count,
        win_rate=round(win_rate, 4),
        profit_factor=round(profit_factor, 3) if profit_factor != float("inf") else 999.0,
        expectancy=round(expectancy, 3),
        total_pnl=round(total_pnl, 2),
        total_fees_paid=round(total_fees, 2),
        total_slippage=round(total_slippage, 4),
        sharpe_ratio=round(sharpe, 3),
        max_drawdown_pct=round(max_dd_pct * 100, 2),
        max_drawdown_usd=round(max_dd_usd, 2),
        current_drawdown_pct=round(curr_dd_pct * 100, 2),
        avg_hold_time_s=round(avg_hold, 0),
        median_hold_time_s=round(med_hold, 0),
        current_streak=cur_streak,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        by_symbol=by_symbol,
        nav=round(wallet_nav, 2),
        cash=round(wallet_cash, 2),
        initial_bankroll=initial_bankroll,
        total_return_pct=round(total_return_pct, 2),
        closes_by_reason=dict(closes_by_reason),
    )


def _empty_analytics(nav: float, cash: float, initial: float, open_count: int) -> PaperAnalytics:
    return PaperAnalytics(
        total_trades=0, open_trades=open_count, win_rate=0.0, profit_factor=0.0,
        expectancy=0.0, total_pnl=0.0, total_fees_paid=0.0, total_slippage=0.0,
        sharpe_ratio=0.0, max_drawdown_pct=0.0, max_drawdown_usd=0.0,
        current_drawdown_pct=0.0, avg_hold_time_s=0.0, median_hold_time_s=0.0,
        current_streak=0, max_win_streak=0, max_loss_streak=0,
        by_symbol={}, nav=nav, cash=cash, initial_bankroll=initial,
        total_return_pct=0.0, closes_by_reason={},
    )


async def _compute_sharpe() -> float:
    """Annualised Sharpe ratio from equity curve (daily returns, risk-free=0)."""
    curve = await database.get_equity_curve(limit=500)
    if len(curve) < 2:
        return 0.0

    navs = [float(r["nav"]) for r in curve]
    # Group into daily buckets — approximate with hourly if fewer points
    if len(navs) < 10:
        return 0.0

    returns = [(navs[i] - navs[i - 1]) / navs[i - 1] for i in range(1, len(navs)) if navs[i - 1] > 0]
    if len(returns) < 5:
        return 0.0

    mean_r = statistics.mean(returns)
    std_r = statistics.stdev(returns) if len(returns) > 1 else 0.0
    if std_r == 0:
        return 0.0

    # Annualise: assuming snapshots every 60s → 525_600 snapshots/year
    periods_per_year = 525_600 / 60   # if snapshot every 60s
    sharpe = (mean_r / std_r) * math.sqrt(periods_per_year)
    return max(-10.0, min(10.0, sharpe))   # clamp to sane range


async def _compute_drawdown(initial_bankroll: float) -> tuple[float, float, float]:
    """Return (max_dd_pct, max_dd_usd, current_dd_pct)."""
    curve = await database.get_equity_curve(limit=500)
    if not curve:
        return 0.0, 0.0, 0.0

    navs = [float(r["nav"]) for r in curve]
    peak = max(navs[0], initial_bankroll)
    max_dd_pct = 0.0
    max_dd_usd = 0.0

    for nav in navs:
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak if peak > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd
            max_dd_usd = peak - nav

    # Current drawdown
    current_nav = navs[-1] if navs else initial_bankroll
    current_peak = max(navs) if navs else initial_bankroll
    curr_dd = (current_peak - current_nav) / current_peak if current_peak > 0 else 0.0

    return max_dd_pct, max_dd_usd, curr_dd


def _compute_streaks(pnl_series: list[float]) -> tuple[int, int, int]:
    """Return (current_streak, max_win_streak, max_loss_streak).
    current_streak: positive = consecutive wins, negative = consecutive losses.
    """
    if not pnl_series:
        return 0, 0, 0

    max_win = max_loss = 0
    cur_win = cur_loss = 0

    for pnl in pnl_series:
        if pnl > 0:
            cur_win += 1
            cur_loss = 0
            max_win = max(max_win, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss = max(max_loss, cur_loss)

    # Current streak = from the end
    current = 0
    for pnl in reversed(pnl_series):
        if current == 0:
            current = 1 if pnl > 0 else -1
        elif (pnl > 0 and current > 0):
            current += 1
        elif (pnl <= 0 and current < 0):
            current -= 1
        else:
            break

    return current, max_win, max_loss


def _compute_by_symbol(
    closed: list[aiosqlite.Row],
    cid_to_sym: dict[str, str],
) -> dict[str, dict]:
    grouped: dict[str, list] = defaultdict(list)
    for r in closed:
        sym = cid_to_sym.get(r["condition_id"], "UNKNOWN")
        grouped[sym].append(r)

    result = {}
    for sym, trades in grouped.items():
        wins = [t for t in trades if float(t["pnl"]) > 0]
        pnls = [float(t["pnl"]) for t in trades]
        hold_times = [float(t["hold_time_s"]) for t in trades if t["hold_time_s"]]
        result[sym] = {
            "total": len(trades),
            "win_rate": round(len(wins) / len(trades), 3) if trades else 0.0,
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(statistics.mean(pnls), 3) if pnls else 0.0,
            "avg_hold_s": round(statistics.mean(hold_times), 0) if hold_times else 0.0,
        }
    return result
