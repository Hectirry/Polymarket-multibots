import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    slug TEXT,
    best_bid REAL,
    best_ask REAL,
    spread REAL,
    bid_depth_usd REAL,
    ask_depth_usd REAL,
    big_bids INTEGER,
    big_asks INTEGER,
    raw_book TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_token_ts ON snapshots(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_snap_slug_ts ON snapshots(slug, ts);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_placed REAL NOT NULL,
    ts_filled REAL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size_usd REAL NOT NULL,
    status TEXT NOT NULL,
    strategy TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_token ON orders(token_id);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def save_snapshot(self, snap: dict) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO snapshots
                (ts, condition_id, token_id, slug, best_bid, best_ask, spread,
                 bid_depth_usd, ask_depth_usd, big_bids, big_asks, raw_book)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap["ts"], snap["condition_id"], snap["token_id"],
                    snap.get("slug"), snap["best_bid"], snap["best_ask"],
                    snap["spread"], snap["bid_depth_usd"], snap["ask_depth_usd"],
                    snap["big_bids"], snap["big_asks"],
                    json.dumps(snap.get("raw_book", {})),
                ),
            )

    def insert_order(self, order: dict) -> int:
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT INTO orders
                (ts_placed, condition_id, token_id, side, price, size_usd,
                 status, strategy, reason)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    order["ts_placed"], order["condition_id"], order["token_id"],
                    order["side"], order["price"], order["size_usd"],
                    order.get("strategy"), order.get("reason"),
                ),
            )
            return cur.lastrowid

    def mark_order_filled(self, order_id: int, ts: float) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE orders SET status='filled', ts_filled=? WHERE id=?",
                (ts, order_id),
            )

    def expire_stale_orders(self, cutoff_ts: float) -> int:
        with self.conn() as c:
            cur = c.execute(
                "UPDATE orders SET status='expired' WHERE status='open' AND ts_placed < ?",
                (cutoff_ts,),
            )
            return cur.rowcount

    def open_orders_for_token(self, token_id: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE token_id=? AND status='open'",
                (token_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def total_open_exposure_usd(self) -> float:
        with self.conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(size_usd), 0) FROM orders WHERE status='open'"
            ).fetchone()
            return float(r[0])
