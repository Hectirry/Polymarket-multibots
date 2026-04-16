"""
core/database.py — SQLite persistence layer via aiosqlite.

All schema creation, insert, and query helpers live here.  The DB is opened
once at startup and shared across the process.  Callers must await init_db()
before using any other function.

Tables added for improved paper trading:
  - equity_curve:   NAV snapshots every 60s (equity curve data)
  - trade_journal:  Full signal context + fill details captured at entry/close
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = "polymarket.db"
_db: Optional[aiosqlite.Connection] = None


async def init_db(path: str = _DB_PATH) -> None:
    """Open the SQLite connection and create tables if they don't exist."""
    global _db, _DB_PATH
    _DB_PATH = path
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")
    await _create_schema(_db)
    await _db.commit()
    logger.info("Database initialised at %s", path)


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """Yield the shared DB connection (must call init_db first)."""
    if _db is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    yield _db


async def _create_schema(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id    TEXT NOT NULL,
            crypto_symbol   TEXT NOT NULL,
            delta           REAL NOT NULL,
            ev_net          REAL NOT NULL,
            our_prob        REAL NOT NULL,
            market_prob     REAL NOT NULL,
            whale_score     REAL NOT NULL DEFAULT 0.0,
            llm_validated   INTEGER NOT NULL DEFAULT 0,
            llm_reason      TEXT NOT NULL DEFAULT '',
            quality_score   REAL NOT NULL DEFAULT 1.0,
            timestamp       REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id                  TEXT PRIMARY KEY,
            condition_id        TEXT NOT NULL,
            side                TEXT NOT NULL,
            size_usd            REAL NOT NULL,
            entry_price         REAL NOT NULL,
            exit_price          REAL,
            pnl                 REAL NOT NULL DEFAULT 0.0,
            status              TEXT NOT NULL DEFAULT 'OPEN',
            close_reason        TEXT,
            open_ts             REAL NOT NULL,
            close_ts            REAL,
            slippage_usd        REAL NOT NULL DEFAULT 0.0,
            fill_fraction       REAL NOT NULL DEFAULT 1.0,
            max_favorable_excursion  REAL NOT NULL DEFAULT 0.0,
            max_adverse_excursion    REAL NOT NULL DEFAULT 0.0,
            hold_time_s         REAL
        );

        CREATE TABLE IF NOT EXISTS whale_events (
            trade_id        TEXT PRIMARY KEY,
            condition_id    TEXT NOT NULL,
            direction       TEXT NOT NULL,
            size_usd        REAL NOT NULL,
            price           REAL NOT NULL,
            timestamp       REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS calibrated_params (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            category        TEXT NOT NULL,
            param_name      TEXT NOT NULL,
            value           REAL NOT NULL,
            updated_at      REAL NOT NULL,
            UNIQUE(category, param_name)
        );

        CREATE TABLE IF NOT EXISTS blocked_categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL UNIQUE,
            reason      TEXT NOT NULL,
            blocked_at  REAL NOT NULL,
            unblock_at  REAL
        );

        CREATE TABLE IF NOT EXISTS equity_curve (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp               REAL NOT NULL,
            nav                     REAL NOT NULL,
            cash                    REAL NOT NULL,
            unrealized_pnl          REAL NOT NULL DEFAULT 0.0,
            realized_pnl_cumulative REAL NOT NULL DEFAULT 0.0,
            open_positions          INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           REAL NOT NULL,
            condition_id        TEXT NOT NULL,
            token_id            TEXT NOT NULL DEFAULT '',
            question            TEXT NOT NULL DEFAULT '',
            crypto_symbol       TEXT NOT NULL DEFAULT '',
            binance_price       REAL,
            market_implied_prob REAL NOT NULL DEFAULT 0.0,
            best_bid            REAL NOT NULL DEFAULT 0.0,
            best_ask            REAL NOT NULL DEFAULT 0.0,
            spread              REAL NOT NULL DEFAULT 0.0,
            volume_24h          REAL NOT NULL DEFAULT 0.0,
            open_interest       REAL NOT NULL DEFAULT 0.0,
            time_to_expiry_s    REAL NOT NULL DEFAULT 0.0,
            whale_score         REAL NOT NULL DEFAULT 0.0,
            whale_count         INTEGER NOT NULL DEFAULT 0,
            ranker_score        REAL,
            ranker_reason       TEXT NOT NULL DEFAULT '',
            verdict             TEXT NOT NULL,
            reject_stage        TEXT NOT NULL DEFAULT '',
            reject_reason       TEXT NOT NULL DEFAULT '',
            signal_delta        REAL,
            ev_net              REAL
        );

        CREATE INDEX IF NOT EXISTS idx_mkt_snap_cid_ts
            ON market_snapshots(condition_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_mkt_snap_verdict_ts
            ON market_snapshots(verdict, timestamp);

        CREATE TABLE IF NOT EXISTS trade_journal (
            trade_id            TEXT PRIMARY KEY,
            condition_id        TEXT NOT NULL,
            crypto_symbol       TEXT NOT NULL,
            question            TEXT NOT NULL,
            side                TEXT NOT NULL,
            delta               REAL NOT NULL,
            ev_net              REAL NOT NULL,
            our_prob            REAL NOT NULL,
            market_prob         REAL NOT NULL,
            whale_score         REAL NOT NULL DEFAULT 0.0,
            depth_score         REAL NOT NULL DEFAULT 0.5,
            quality_score       REAL NOT NULL DEFAULT 1.0,
            llm_validated       INTEGER NOT NULL DEFAULT 0,
            llm_reason          TEXT NOT NULL DEFAULT '',
            requested_price     REAL NOT NULL,
            fill_price          REAL NOT NULL,
            slippage            REAL NOT NULL DEFAULT 0.0,
            fill_fraction       REAL NOT NULL DEFAULT 1.0,
            size_usd            REAL NOT NULL,
            fee_usd             REAL NOT NULL DEFAULT 0.0,
            open_ts             REAL NOT NULL,
            close_reason        TEXT,
            hold_time_s         REAL,
            exit_price          REAL,
            pnl                 REAL,
            max_favorable_excursion  REAL,
            max_adverse_excursion    REAL
        );
    """)


# ─── Signals ──────────────────────────────────────────────────────────────────

async def insert_signal(
    condition_id: str,
    crypto_symbol: str,
    delta: float,
    ev_net: float,
    our_prob: float,
    market_prob: float,
    whale_score: float,
    llm_validated: bool,
    llm_reason: str,
    quality_score: float,
    timestamp: float,
) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT INTO signals
               (condition_id, crypto_symbol, delta, ev_net, our_prob, market_prob,
                whale_score, llm_validated, llm_reason, quality_score, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (condition_id, crypto_symbol, delta, ev_net, our_prob, market_prob,
             whale_score, int(llm_validated), llm_reason, quality_score, timestamp),
        )
        await db.commit()


async def get_recent_signals(limit: int = 100) -> list[aiosqlite.Row]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        return await cursor.fetchall()


# ─── Trades ───────────────────────────────────────────────────────────────────

async def insert_trade(
    trade_id: str,
    condition_id: str,
    side: str,
    size_usd: float,
    entry_price: float,
    open_ts: float,
    slippage_usd: float = 0.0,
    fill_fraction: float = 1.0,
) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO trades
               (id, condition_id, side, size_usd, entry_price, status, open_ts,
                slippage_usd, fill_fraction)
               VALUES (?,?,?,?,?,'OPEN',?,?,?)""",
            (trade_id, condition_id, side, size_usd, entry_price, open_ts,
             slippage_usd, fill_fraction),
        )
        await db.commit()


async def update_trade_close(
    trade_id: str,
    exit_price: float,
    pnl: float,
    close_ts: float,
    close_reason: str = "manual",
    hold_time_s: float = 0.0,
    max_favorable_excursion: float = 0.0,
    max_adverse_excursion: float = 0.0,
) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE trades
               SET exit_price=?, pnl=?, status='CLOSED', close_ts=?,
                   close_reason=?, hold_time_s=?,
                   max_favorable_excursion=?, max_adverse_excursion=?
               WHERE id=?""",
            (exit_price, pnl, close_ts, close_reason, hold_time_s,
             max_favorable_excursion, max_adverse_excursion, trade_id),
        )
        await db.commit()


async def get_open_trades() -> list[aiosqlite.Row]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY open_ts DESC"
        )
        return await cursor.fetchall()


async def get_all_trades(limit: int = 200) -> list[aiosqlite.Row]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM trades ORDER BY open_ts DESC LIMIT ?", (limit,)
        )
        return await cursor.fetchall()


async def get_closed_trades_for_symbol(symbol: str) -> list[aiosqlite.Row]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT t.* FROM trades t
               JOIN signals s ON s.condition_id = t.condition_id
               WHERE t.status='CLOSED' AND s.crypto_symbol=?
               ORDER BY t.close_ts DESC""",
            (symbol,),
        )
        return await cursor.fetchall()


async def get_closed_trades_stats() -> dict[str, Any]:
    """Return aggregate stats for analytics — single query for efficiency."""
    async with get_db() as db:
        cursor = await db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_profit,
                SUM(CASE WHEN pnl <= 0 THEN ABS(pnl) ELSE 0 END) as gross_loss,
                SUM(pnl) as total_pnl,
                AVG(hold_time_s) as avg_hold_time_s,
                SUM(slippage_usd) as total_slippage
            FROM trades WHERE status='CLOSED'
        """)
        return dict(await cursor.fetchone())


# ─── Whale Events ─────────────────────────────────────────────────────────────

async def insert_whale_event(
    trade_id: str,
    condition_id: str,
    direction: str,
    size_usd: float,
    price: float,
    timestamp: float,
) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO whale_events
               (trade_id, condition_id, direction, size_usd, price, timestamp)
               VALUES (?,?,?,?,?,?)""",
            (trade_id, condition_id, direction, size_usd, price, timestamp),
        )
        await db.commit()


async def get_recent_whale_events(limit: int = 50) -> list[aiosqlite.Row]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM whale_events ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        return await cursor.fetchall()


# ─── Calibrated Params ────────────────────────────────────────────────────────

async def upsert_calibrated_param(
    category: str, param_name: str, value: float, updated_at: float
) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT INTO calibrated_params (category, param_name, value, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(category, param_name) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
            (category, param_name, value, updated_at),
        )
        await db.commit()


# ─── Blocked Categories ───────────────────────────────────────────────────────

async def is_category_blocked(category: str, now: float) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT 1 FROM blocked_categories
               WHERE category=? AND (unblock_at IS NULL OR unblock_at > ?)""",
            (category, now),
        )
        return await cursor.fetchone() is not None


# ─── Equity Curve ─────────────────────────────────────────────────────────────

async def insert_equity_point(
    timestamp: float,
    nav: float,
    cash: float,
    unrealized_pnl: float,
    realized_pnl_cumulative: float,
    open_positions: int,
) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT INTO equity_curve
               (timestamp, nav, cash, unrealized_pnl, realized_pnl_cumulative, open_positions)
               VALUES (?,?,?,?,?,?)""",
            (timestamp, nav, cash, unrealized_pnl, realized_pnl_cumulative, open_positions),
        )
        await db.commit()


async def get_equity_curve(limit: int = 500) -> list[aiosqlite.Row]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM equity_curve ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
    return list(reversed(rows))  # chronological order


# ─── Trade Journal ────────────────────────────────────────────────────────────

async def insert_journal_entry(
    trade_id: str,
    condition_id: str,
    crypto_symbol: str,
    question: str,
    side: str,
    delta: float,
    ev_net: float,
    our_prob: float,
    market_prob: float,
    whale_score: float,
    depth_score: float,
    quality_score: float,
    llm_validated: bool,
    llm_reason: str,
    requested_price: float,
    fill_price: float,
    slippage: float,
    fill_fraction: float,
    size_usd: float,
    fee_usd: float,
    open_ts: float,
) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO trade_journal
               (trade_id, condition_id, crypto_symbol, question, side,
                delta, ev_net, our_prob, market_prob, whale_score, depth_score,
                quality_score, llm_validated, llm_reason,
                requested_price, fill_price, slippage, fill_fraction,
                size_usd, fee_usd, open_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, condition_id, crypto_symbol, question, side,
             delta, ev_net, our_prob, market_prob, whale_score, depth_score,
             quality_score, int(llm_validated), llm_reason,
             requested_price, fill_price, slippage, fill_fraction,
             size_usd, fee_usd, open_ts),
        )
        await db.commit()


async def update_journal_close(
    trade_id: str,
    close_reason: str,
    hold_time_s: float,
    exit_price: float,
    pnl: float,
    max_favorable_excursion: float,
    max_adverse_excursion: float,
) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE trade_journal SET
               close_reason=?, hold_time_s=?, exit_price=?, pnl=?,
               max_favorable_excursion=?, max_adverse_excursion=?
               WHERE trade_id=?""",
            (close_reason, hold_time_s, exit_price, pnl,
             max_favorable_excursion, max_adverse_excursion, trade_id),
        )
        await db.commit()


async def get_journal(limit: int = 100) -> list[aiosqlite.Row]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM trade_journal ORDER BY open_ts DESC LIMIT ?", (limit,)
        )
        return await cursor.fetchall()


async def insert_market_snapshot(
    timestamp: float,
    condition_id: str,
    token_id: str,
    question: str,
    crypto_symbol: str,
    binance_price: Optional[float],
    market_implied_prob: float,
    best_bid: float,
    best_ask: float,
    spread: float,
    volume_24h: float,
    open_interest: float,
    time_to_expiry_s: float,
    whale_score: float,
    whale_count: int,
    ranker_score: Optional[float],
    ranker_reason: str,
    verdict: str,
    reject_stage: str,
    reject_reason: str,
    signal_delta: Optional[float],
    ev_net: Optional[float],
) -> None:
    async with get_db() as db:
        await db.execute(
            """INSERT INTO market_snapshots
               (timestamp, condition_id, token_id, question, crypto_symbol,
                binance_price, market_implied_prob, best_bid, best_ask, spread,
                volume_24h, open_interest, time_to_expiry_s,
                whale_score, whale_count, ranker_score, ranker_reason,
                verdict, reject_stage, reject_reason, signal_delta, ev_net)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (timestamp, condition_id, token_id, question, crypto_symbol,
             binance_price, market_implied_prob, best_bid, best_ask, spread,
             volume_24h, open_interest, time_to_expiry_s,
             whale_score, whale_count, ranker_score, ranker_reason,
             verdict, reject_stage, reject_reason, signal_delta, ev_net),
        )
        await db.commit()


async def get_snapshot_stats(since_ts: float) -> dict[str, Any]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT verdict, reject_stage, COUNT(*) as n
               FROM market_snapshots
               WHERE timestamp >= ?
               GROUP BY verdict, reject_stage
               ORDER BY n DESC""",
            (since_ts,),
        )
        rows = await cursor.fetchall()
    return {"buckets": [dict(r) for r in rows]}


async def get_pnl_series() -> list[float]:
    """Return chronologically ordered list of per-trade PnL (closed only)."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT pnl FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL ORDER BY close_ts ASC"
        )
        return [row[0] for row in await cursor.fetchall()]
