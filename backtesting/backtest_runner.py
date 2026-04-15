"""
backtesting/backtest_runner.py — Historical signal recalibration.

Loads closed trades from the database and computes per-category win rates.
When a category's sample count exceeds setup_quality_min_sample, updates
calibrated_params in the DB so signal_router uses tuned thresholds.

Invoked automatically every 300s from main.py, or manually with --backtest-only.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from core import database
from core.config import AppConfig

logger = logging.getLogger(__name__)


async def recalibrate(cfg: AppConfig) -> dict[str, float]:
    """
    Compute per-symbol win rates from closed trades and persist to DB.

    Returns: {symbol: win_rate}
    """
    rows = await database.get_all_trades(limit=5_000)
    closed = [r for r in rows if r["status"] == "CLOSED"]

    if not closed:
        logger.info("Recalibration: no closed trades yet")
        return {}

    # Group by crypto_symbol via signals join (approximation: use condition_id prefix)
    # A more accurate approach reads the signals table for each trade.
    signal_rows = await database.get_recent_signals(limit=5_000)
    cid_to_sym: dict[str, str] = {r["condition_id"]: r["crypto_symbol"] for r in signal_rows}

    wins_by_sym: dict[str, list[bool]] = defaultdict(list)
    for row in closed:
        sym = cid_to_sym.get(row["condition_id"], "UNKNOWN")
        wins_by_sym[sym].append(float(row["pnl"]) > 0)

    results: dict[str, float] = {}
    ts = time.time()
    for sym, outcomes in wins_by_sym.items():
        if len(outcomes) < cfg.setup_quality_min_sample:
            continue
        wr = sum(outcomes) / len(outcomes)
        results[sym] = wr
        await database.upsert_calibrated_param(sym, "win_rate", wr, ts)
        await database.upsert_calibrated_param(sym, "sample_count", float(len(outcomes)), ts)
        logger.info("Calibrated %s: win_rate=%.3f (n=%d)", sym, wr, len(outcomes))

    return results


async def get_setup_stats(condition_id: str, symbol: str) -> tuple[float | None, int]:
    """Return (win_rate, sample_count) for a symbol from calibrated_params."""
    # Not doing a per-market lookup — use symbol-level win rate
    rows = await database.get_all_trades(limit=5_000)
    signal_rows = await database.get_recent_signals(limit=5_000)
    cid_to_sym = {r["condition_id"]: r["crypto_symbol"] for r in signal_rows}

    trades = [r for r in rows if r["status"] == "CLOSED"
              and cid_to_sym.get(r["condition_id"], "") == symbol]
    if not trades:
        return None, 0

    wins = sum(1 for t in trades if float(t["pnl"]) > 0)
    return wins / len(trades), len(trades)
