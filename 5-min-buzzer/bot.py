import argparse
import logging
import signal
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from market import PolymarketClient, summarize_book
from storage import Storage
from strategy import StinkBid


def setup_logging(log_dir: str, level: str) -> None:
    Path(log_dir).mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(f"{log_dir}/bot.log"),
        ],
    )


class Bot:
    def __init__(self, cfg: dict, mode: str):
        self.cfg = cfg
        self.mode = mode
        self.storage = Storage(cfg["bot"]["db_path"])
        self.client = PolymarketClient()
        self.strategy = StinkBid(cfg["strategy"])
        self.poll = float(cfg["bot"]["poll_interval_s"])
        self.big_bid_usd = float(cfg["strategy"]["big_bid_usd"])
        self.bet_size = float(cfg["strategy"]["bet_size_usd"])
        self.max_exposure = float(cfg["strategy"]["max_exposure_usd"])
        self.running = True
        self.log = logging.getLogger("bot")

    def stop(self, *_) -> None:
        self.log.info("shutdown signal received")
        self.running = False

    def run(self) -> None:
        self.log.info(f"starting bot in {self.mode} mode")
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        while self.running:
            t0 = time.time()
            try:
                self.tick()
            except Exception as e:
                self.log.exception(f"tick failed: {e}")
            elapsed = time.time() - t0
            time.sleep(max(0.0, self.poll - elapsed))
        self.client.close()
        self.log.info("bot stopped")

    def tick(self) -> None:
        mcfg = self.cfg["market"]
        markets = self.client.discover_markets(
            slug_contains=[s.lower() for s in mcfg["slug_contains"]],
            min_volume_24h=float(mcfg["min_volume_24h"]),
            max_ttc_s=float(mcfg["max_time_to_close_s"]),
        )
        self.log.info(f"discovered {len(markets)} candidate markets")

        for m in markets:
            self.handle_market(m)

        if self.mode == "paper":
            self.expire_stale()

    def handle_market(self, m: dict) -> None:
        token_id = m["yes_token_id"]
        book = self.client.get_orderbook(token_id)
        if not book:
            return
        summary = summarize_book(book, self.big_bid_usd)

        self.storage.save_snapshot({
            "ts": time.time(),
            "condition_id": m["condition_id"],
            "token_id": token_id,
            "slug": m["slug"],
            **summary,
            "raw_book": book,
        })

        if self.mode == "paper":
            self.check_fills(token_id, summary)
            self.maybe_place_order(m, summary)

    def maybe_place_order(self, m: dict, summary: dict) -> None:
        if self.storage.open_orders_for_token(m["yes_token_id"]):
            return
        if self.storage.total_open_exposure_usd() + self.bet_size > self.max_exposure:
            return
        intent = self.strategy.evaluate(m, summary)
        if not intent:
            return
        oid = self.storage.insert_order({
            "ts_placed": time.time(),
            "condition_id": intent.condition_id,
            "token_id": intent.token_id,
            "side": intent.side,
            "price": intent.price,
            "size_usd": intent.size_usd,
            "strategy": self.strategy.name,
            "reason": intent.reason,
        })
        self.log.info(
            f"[PAPER] order #{oid} {intent.side} {intent.token_id[:10]} "
            f"@ {intent.price} ${intent.size_usd:.0f} :: {intent.reason}"
        )

    def check_fills(self, token_id: str, summary: dict) -> None:
        best_ask = summary["best_ask"]
        for o in self.storage.open_orders_for_token(token_id):
            if o["side"] == "BUY" and best_ask <= o["price"]:
                self.storage.mark_order_filled(o["id"], time.time())
                self.log.info(
                    f"[PAPER] FILLED #{o['id']} ask={best_ask:.3f} bid={o['price']:.3f}"
                )

    def expire_stale(self) -> None:
        cutoff = time.time() - float(self.cfg["market"]["max_time_to_close_s"])
        n = self.storage.expire_stale_orders(cutoff)
        if n:
            self.log.info(f"expired {n} stale orders")


def load_env() -> None:
    for envp in (".env", "../.env"):
        if Path(envp).exists():
            load_dotenv(envp)
            return


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--mode", choices=["observe", "paper"], default="observe")
    args = p.parse_args()

    load_env()
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    setup_logging(cfg["bot"]["log_dir"], cfg["bot"].get("log_level", "INFO"))
    Bot(cfg, args.mode).run()


if __name__ == "__main__":
    main()
