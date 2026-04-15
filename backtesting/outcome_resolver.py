"""
backtesting/outcome_resolver.py — Resolves paper trades against historical outcomes.

Used in --backtest-only mode to replay market resolutions and compute
simulated P&L without live trading.
"""

from __future__ import annotations

import logging
import time

import aiohttp

from core import database

logger = logging.getLogger(__name__)

_CLOB_BASE = "https://clob.polymarket.com"


async def resolve_open_trades_from_api() -> int:
    """
    Check all open paper trades against Polymarket resolution data.
    Returns the count of trades successfully resolved.
    """
    open_trades = await database.get_open_trades()
    if not open_trades:
        return 0

    resolved_count = 0
    async with aiohttp.ClientSession() as session:
        for row in open_trades:
            cid = row["condition_id"]
            result = await _fetch_resolution(session, cid)
            if result is None:
                continue
            resolution_price, close_ts = result
            # Calculate PnL
            entry = float(row["entry_price"])
            size = float(row["size_usd"])
            side = row["side"]
            contracts = size / entry if entry > 0 else 0
            if side == "YES":
                pnl = contracts * resolution_price - size
            else:
                pnl = contracts * (1.0 - resolution_price) - size

            await database.update_trade_close(row["id"], resolution_price, pnl, close_ts)
            resolved_count += 1
            logger.info(
                "Resolved trade %s: exit=%.4f pnl=%+.2f", row["id"], resolution_price, pnl
            )

    return resolved_count


async def _fetch_resolution(
    session: aiohttp.ClientSession, condition_id: str
) -> tuple[float, float] | None:
    url = f"{_CLOB_BASE}/markets/{condition_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        resolved = data.get("closed") or data.get("resolved")
        resolution_price = data.get("resolution_price")
        if resolved and resolution_price is not None:
            return float(resolution_price), time.time()
        return None
    except Exception as exc:
        logger.debug("Resolution fetch failed for %s: %s", condition_id, exc)
        return None
