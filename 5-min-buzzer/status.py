import argparse
import sqlite3
from datetime import datetime


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="buzzer.db")
    args = p.parse_args()

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    now = datetime.now().timestamp()

    print("=" * 64)
    print(f" 5-MIN BUZZER  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 64)

    n, last = c.execute(
        "SELECT COUNT(*), COALESCE(MAX(ts), 0) FROM snapshots"
    ).fetchone()
    print(f"\nSnapshots: {n}")
    if last:
        print(f"Last snapshot: {now - last:.1f}s ago")

    print("\n-- OPEN ORDERS --")
    rows = c.execute(
        "SELECT * FROM orders WHERE status='open' ORDER BY ts_placed DESC"
    ).fetchall()
    for r in rows:
        age = now - r["ts_placed"]
        print(
            f"  #{r['id']:<4} {r['side']:<4} {r['token_id'][:10]} "
            f"@ {r['price']:.3f}  ${r['size_usd']:>5.0f}  "
            f"({age:>4.0f}s)  {r['reason']}"
        )
    if not rows:
        print("  (none)")

    print("\n-- RECENT FILLS (last 10) --")
    rows = c.execute(
        "SELECT * FROM orders WHERE status='filled' "
        "ORDER BY ts_filled DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        ttf = (r["ts_filled"] or 0) - r["ts_placed"]
        print(
            f"  #{r['id']:<4} {r['side']:<4} {r['token_id'][:10]} "
            f"@ {r['price']:.3f}  ${r['size_usd']:>5.0f}  "
            f"filled in {ttf:>4.0f}s"
        )
    if not rows:
        print("  (none)")

    print("\n-- MARKETS OBSERVED (last 1h) --")
    rows = c.execute(
        """
        SELECT slug, COUNT(*) AS n,
               AVG(spread) AS avg_spread,
               AVG(big_bids) AS avg_big_bids,
               MIN(best_ask) AS min_ask, MAX(best_ask) AS max_ask
        FROM snapshots WHERE ts > ?
        GROUP BY slug ORDER BY n DESC LIMIT 10
        """,
        (now - 3600,),
    ).fetchall()
    for r in rows:
        print(
            f"  {(r['slug'] or '')[:42]:42}  n={r['n']:>4}  "
            f"spread={r['avg_spread']:.3f}  big_bids={r['avg_big_bids']:.1f}  "
            f"ask∈[{r['min_ask']:.2f},{r['max_ask']:.2f}]"
        )
    if not rows:
        print("  (none)")

    stats = c.execute(
        """
        SELECT
          SUM(CASE WHEN status='open'     THEN 1 ELSE 0 END) AS open_,
          SUM(CASE WHEN status='filled'   THEN 1 ELSE 0 END) AS filled,
          SUM(CASE WHEN status='expired'  THEN 1 ELSE 0 END) AS expired,
          COUNT(*) AS total
        FROM orders
        """
    ).fetchone()
    print("\n-- ORDER STATS --")
    print(
        f"  total={stats['total']}  open={stats['open_']}  "
        f"filled={stats['filled']}  expired={stats['expired']}"
    )
    if stats["total"]:
        fill_rate = (stats["filled"] or 0) / stats["total"] * 100
        print(f"  fill_rate={fill_rate:.1f}%")

    c.close()


if __name__ == "__main__":
    main()
