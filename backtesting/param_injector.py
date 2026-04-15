"""
backtesting/param_injector.py — Loads calibrated params into AppConfig at runtime.

After recalibrate() runs, call inject_calibrated_params(cfg) to update
the in-memory config with DB-sourced per-symbol tuned thresholds.
Currently updates category_overrides["SYMBOL"]["min_delta"] based on
observed edge size.
"""

from __future__ import annotations

import logging

import aiosqlite

from core import database
from core.config import AppConfig

logger = logging.getLogger(__name__)


async def inject_calibrated_params(cfg: AppConfig) -> None:
    """Merge DB calibrated_params into cfg.category_overrides."""
    async with database.get_db() as db:
        cursor = await db.execute(
            "SELECT category, param_name, value FROM calibrated_params"
        )
        rows: list[aiosqlite.Row] = await cursor.fetchall()

    for row in rows:
        sym = row["category"]
        param = row["param_name"]
        value = row["value"]

        if param == "win_rate":
            # If win rate is low, tighten min_delta for this symbol
            overrides = cfg.category_overrides.setdefault(sym, {})
            if value < cfg.setup_quality_min_wr:
                tighter_delta = min(
                    overrides.get("min_delta", cfg.min_delta) * 1.1, 0.40
                )
                overrides["min_delta"] = round(tighter_delta, 4)
                logger.info(
                    "ParamInjector: tightened %s min_delta → %.4f (win_rate=%.3f)",
                    sym, tighter_delta, value,
                )
            else:
                logger.debug("ParamInjector: %s win_rate=%.3f — no adjustment needed", sym, value)
