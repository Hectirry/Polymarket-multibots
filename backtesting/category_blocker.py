"""
backtesting/category_blocker.py — Automatic category blocking on poor performance.

If a symbol's rolling win rate falls below a configurable floor (default 40%)
over at least 20 trades, the category is blocked for 24 hours.
"""

from __future__ import annotations

import logging
import time

from core import database
from core.config import AppConfig

logger = logging.getLogger(__name__)

_BLOCK_DURATION_S = 86_400   # 24 hours
_BLOCK_WR_FLOOR = 0.40
_BLOCK_MIN_SAMPLE = 20


async def check_and_block_categories(cfg: AppConfig) -> None:
    """Block categories with persistently poor performance."""
    rows = await database.get_all_trades(limit=5_000)
    signal_rows = await database.get_recent_signals(limit=5_000)
    cid_to_sym = {r["condition_id"]: r["crypto_symbol"] for r in signal_rows}

    from collections import defaultdict
    sym_outcomes: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        if row["status"] != "CLOSED":
            continue
        sym = cid_to_sym.get(row["condition_id"], "")
        if sym:
            sym_outcomes[sym].append(float(row["pnl"]) > 0)

    now = time.time()
    for sym, outcomes in sym_outcomes.items():
        if len(outcomes) < _BLOCK_MIN_SAMPLE:
            continue
        wr = sum(outcomes) / len(outcomes)
        if wr < _BLOCK_WR_FLOOR:
            already_blocked = await database.is_category_blocked(sym, now)
            if not already_blocked:
                async with database.get_db() as db:
                    await db.execute(
                        """INSERT OR REPLACE INTO blocked_categories
                           (category, reason, blocked_at, unblock_at)
                           VALUES (?,?,?,?)""",
                        (sym, f"win_rate={wr:.2%} < {_BLOCK_WR_FLOOR:.0%}", now, now + _BLOCK_DURATION_S),
                    )
                    await db.commit()
                logger.warning(
                    "CategoryBlocker: blocked %s for 24h (win_rate=%.2%%, n=%d)",
                    sym, wr * 100, len(outcomes),
                )
