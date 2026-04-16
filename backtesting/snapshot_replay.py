"""
backtesting/snapshot_replay.py — Replay captured market snapshots.

Reads rows from the market_snapshots table (captured live by main.py) and
reports distribution of router decisions. This is the starting point for a
real backtest: once you have enough snapshots AND the trades resolve, you
can join against `trades` to compute hypothetical PnL under different
configs.

Usage:
    python -m backtesting.snapshot_replay --hours 24
    python -m backtesting.snapshot_replay --since 1776200000 --top-reasons 10

For now this is descriptive only — it does NOT re-run the router with a
different config. That's the next iteration.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter

import aiosqlite


async def main(since_ts: float, top_n: int, db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        total = (await (await db.execute(
            "SELECT COUNT(*) FROM market_snapshots WHERE timestamp >= ?",
            (since_ts,),
        )).fetchone())[0]

        accepted = (await (await db.execute(
            "SELECT COUNT(*) FROM market_snapshots WHERE timestamp >= ? AND verdict='accepted'",
            (since_ts,),
        )).fetchone())[0]

        unique_mkts = (await (await db.execute(
            "SELECT COUNT(DISTINCT condition_id) FROM market_snapshots WHERE timestamp >= ?",
            (since_ts,),
        )).fetchone())[0]

        stages = await (await db.execute(
            """SELECT reject_stage, COUNT(*) n
               FROM market_snapshots
               WHERE timestamp >= ? AND verdict='rejected'
               GROUP BY reject_stage ORDER BY n DESC""",
            (since_ts,),
        )).fetchall()

        ranker_rows = await (await db.execute(
            """SELECT AVG(ranker_score), MIN(ranker_score), MAX(ranker_score),
                      SUM(CASE WHEN ranker_score IS NOT NULL THEN 1 ELSE 0 END)
               FROM market_snapshots WHERE timestamp >= ?""",
            (since_ts,),
        )).fetchone()

        by_symbol = await (await db.execute(
            """SELECT crypto_symbol,
                      COUNT(*) as total,
                      SUM(CASE WHEN verdict='accepted' THEN 1 ELSE 0 END) as accepted
               FROM market_snapshots WHERE timestamp >= ?
               GROUP BY crypto_symbol ORDER BY total DESC""",
            (since_ts,),
        )).fetchall()

    print(f"\n=== Snapshot replay (since ts={since_ts:.0f}) ===")
    print(f"Total snapshots:    {total}")
    print(f"Unique markets:     {unique_mkts}")
    print(f"Accepted:           {accepted}  ({(accepted/total*100 if total else 0):.1f}%)")
    print(f"Rejected:           {total - accepted}")

    if ranker_rows and ranker_rows[3]:
        avg, mn, mx, n = ranker_rows
        print(f"\nRanker scores ({n} scored):  avg={avg:.2f}  min={mn:.2f}  max={mx:.2f}")
    else:
        print("\nRanker scores: none recorded (ranker disabled or no key)")

    print("\n--- Reject stages ---")
    for i, row in enumerate(stages):
        if i >= top_n:
            break
        stage = row["reject_stage"] or "(none)"
        print(f"  {stage:16s}  {row['n']}")

    print("\n--- By symbol ---")
    print(f"  {'symbol':10s}  {'total':>6s}  {'accepted':>10s}  {'acc%':>7s}")
    for row in by_symbol:
        sym = row["crypto_symbol"] or "(unknown)"
        t = row["total"]; a = row["accepted"]
        pct = (a / t * 100) if t else 0
        print(f"  {sym:10s}  {t:>6d}  {a:>10d}  {pct:>6.1f}%")
    print()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay market snapshots")
    p.add_argument("--hours", type=float, default=24.0,
                   help="Look back window in hours (default: 24)")
    p.add_argument("--since", type=float, default=None,
                   help="Explicit unix timestamp (overrides --hours)")
    p.add_argument("--top-reasons", type=int, default=10,
                   help="How many reject stages to show")
    p.add_argument("--db", default="polymarket.db")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    since = args.since if args.since else (time.time() - args.hours * 3600)
    asyncio.run(main(since_ts=since, top_n=args.top_reasons, db_path=args.db))
