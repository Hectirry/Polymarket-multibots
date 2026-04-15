"""tests/test_schema.py — SQLite schema and CRUD operation tests."""

from __future__ import annotations

import time

import pytest

from core import database


@pytest.fixture(autouse=True)
async def fresh_db(tmp_path):
    """Give each test a clean temporary database."""
    db_path = str(tmp_path / "test_schema.db")
    await database.init_db(db_path)
    yield
    await database.close_db()


# ── test_database_schema_creation ─────────────────────────────────────────────

class TestDatabaseSchemaCreation:
    @pytest.mark.asyncio
    async def test_all_tables_exist(self):
        async with database.get_db() as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {r[0] for r in await cursor.fetchall()}

        expected = {
            "signals",
            "trades",
            "whale_events",
            "calibrated_params",
            "blocked_categories",
        }
        assert expected.issubset(tables)

    @pytest.mark.asyncio
    async def test_signals_table_columns(self):
        async with database.get_db() as db:
            cursor = await db.execute("PRAGMA table_info(signals)")
            cols = {r[1] for r in await cursor.fetchall()}

        required = {
            "condition_id", "crypto_symbol", "delta", "ev_net", "our_prob",
            "market_prob", "whale_score", "llm_validated", "llm_reason",
            "quality_score", "timestamp",
        }
        assert required.issubset(cols)

    @pytest.mark.asyncio
    async def test_trades_table_columns(self):
        async with database.get_db() as db:
            cursor = await db.execute("PRAGMA table_info(trades)")
            cols = {r[1] for r in await cursor.fetchall()}

        required = {
            "id", "condition_id", "side", "size_usd", "entry_price",
            "exit_price", "pnl", "status", "open_ts", "close_ts",
        }
        assert required.issubset(cols)

    @pytest.mark.asyncio
    async def test_whale_events_table_columns(self):
        async with database.get_db() as db:
            cursor = await db.execute("PRAGMA table_info(whale_events)")
            cols = {r[1] for r in await cursor.fetchall()}

        required = {"trade_id", "condition_id", "direction", "size_usd", "price", "timestamp"}
        assert required.issubset(cols)

    @pytest.mark.asyncio
    async def test_schema_is_idempotent(self):
        """Calling init_db a second time should not fail (CREATE TABLE IF NOT EXISTS)."""
        async with database.get_db() as db:
            await database._create_schema(db)  # second call — should be safe


# ── test_whale_event_insert ───────────────────────────────────────────────────

class TestWhaleEventInsert:
    @pytest.mark.asyncio
    async def test_insert_and_retrieve_whale_event(self):
        now = time.time()
        await database.insert_whale_event(
            trade_id="whale-001",
            condition_id="0xabc123",
            direction="YES",
            size_usd=2_500.0,
            price=0.45,
            timestamp=now,
        )

        rows = await database.get_recent_whale_events(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["trade_id"] == "whale-001"
        assert row["condition_id"] == "0xabc123"
        assert row["direction"] == "YES"
        assert float(row["size_usd"]) == pytest.approx(2_500.0)

    @pytest.mark.asyncio
    async def test_duplicate_trade_id_ignored(self):
        now = time.time()
        await database.insert_whale_event("whale-dup", "0xdup", "NO", 1_000.0, 0.55, now)
        await database.insert_whale_event("whale-dup", "0xdup", "NO", 1_000.0, 0.55, now)

        rows = await database.get_recent_whale_events(limit=10)
        assert len(rows) == 1  # INSERT OR IGNORE deduplicated

    @pytest.mark.asyncio
    async def test_insert_and_retrieve_signal(self):
        now = time.time()
        await database.insert_signal(
            condition_id="0xsig001",
            crypto_symbol="ETH",
            delta=0.18,
            ev_net=0.10,
            our_prob=0.63,
            market_prob=0.45,
            whale_score=0.75,
            llm_validated=True,
            llm_reason="Strong signal",
            quality_score=1.0,
            timestamp=now,
        )

        rows = await database.get_recent_signals(limit=10)
        assert len(rows) == 1
        assert rows[0]["crypto_symbol"] == "ETH"
        assert float(rows[0]["delta"]) == pytest.approx(0.18)
        assert bool(rows[0]["llm_validated"]) is True

    @pytest.mark.asyncio
    async def test_insert_trade_and_close(self):
        now = time.time()
        await database.insert_trade(
            trade_id="trade-close-001",
            condition_id="0xclose001",
            side="YES",
            size_usd=40.0,
            entry_price=0.459,
            open_ts=now,
        )

        open_trades = await database.get_open_trades()
        assert any(r["id"] == "trade-close-001" for r in open_trades)

        await database.update_trade_close("trade-close-001", 1.0, 47.12, now + 3600)

        open_trades_after = await database.get_open_trades()
        assert not any(r["id"] == "trade-close-001" for r in open_trades_after)

        all_trades = await database.get_all_trades(limit=10)
        closed = next(r for r in all_trades if r["id"] == "trade-close-001")
        assert closed["status"] == "CLOSED"
        assert float(closed["pnl"]) == pytest.approx(47.12)

    @pytest.mark.asyncio
    async def test_category_blocked_detection(self):
        now = time.time()
        async with database.get_db() as db:
            await db.execute(
                """INSERT INTO blocked_categories (category, reason, blocked_at, unblock_at)
                   VALUES ('BTC', 'poor win rate', ?, ?)""",
                (now, now + 86_400),
            )
            await db.commit()

        assert await database.is_category_blocked("BTC", now) is True
        assert await database.is_category_blocked("ETH", now) is False
        # After unblock_at it should no longer be blocked
        assert await database.is_category_blocked("BTC", now + 90_000) is False
