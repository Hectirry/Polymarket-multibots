import json
import logging
import time
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


class PolymarketClient:
    def __init__(self, timeout: float = 10.0):
        self.http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "5min-buzzer/0.1"},
        )

    def close(self) -> None:
        self.http.close()

    def discover_markets(
        self,
        slug_contains: list[str],
        min_volume_24h: float,
        max_ttc_s: float,
    ) -> list[dict]:
        params = {
            "active": "true",
            "closed": "false",
            "limit": 500,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            r = self.http.get(f"{GAMMA}/markets", params=params)
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            log.warning(f"gamma fetch failed: {e}")
            return []

        now = time.time()
        out = []
        for m in raw:
            slug = (m.get("slug") or "").lower()
            if not any(s in slug for s in slug_contains):
                continue
            try:
                vol = float(m.get("volume24hr") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            if vol < min_volume_24h:
                continue

            end_date = m.get("endDate") or m.get("endDateIso")
            ttc = None
            if end_date:
                try:
                    end_ts = datetime.fromisoformat(
                        end_date.replace("Z", "+00:00")
                    ).timestamp()
                    ttc = end_ts - now
                except Exception:
                    ttc = None
            if ttc is None or ttc < 0 or ttc > max_ttc_s:
                continue

            tokens = m.get("clobTokenIds") or []
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    tokens = []
            yes_token = tokens[0] if len(tokens) > 0 else None
            no_token = tokens[1] if len(tokens) > 1 else None
            if not yes_token:
                continue

            out.append({
                "condition_id": m.get("conditionId") or m.get("id") or "",
                "slug": slug,
                "question": m.get("question", ""),
                "volume_24h": vol,
                "time_to_close_s": ttc,
                "yes_token_id": yes_token,
                "no_token_id": no_token,
            })
        return out

    def get_orderbook(self, token_id: str) -> dict | None:
        try:
            r = self.http.get(f"{CLOB}/book", params={"token_id": token_id})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.debug(f"orderbook fetch failed for {token_id[:10]}: {e}")
            return None


def summarize_book(book: dict, big_bid_usd: float) -> dict:
    def parse_level(lv):
        try:
            return float(lv["price"]), float(lv["size"])
        except (KeyError, TypeError, ValueError):
            return None

    bids = [x for x in (parse_level(b) for b in book.get("bids") or []) if x]
    asks = [x for x in (parse_level(a) for a in book.get("asks") or []) if x]
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])

    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 1.0
    spread = max(0.0, best_ask - best_bid)

    bid_depth_usd = sum(p * s for p, s in bids[:5])
    ask_depth_usd = sum(p * s for p, s in asks[:5])
    big_bids = sum(1 for p, s in bids if p * s >= big_bid_usd)
    big_asks = sum(1 for p, s in asks if p * s >= big_bid_usd)

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "bid_depth_usd": bid_depth_usd,
        "ask_depth_usd": ask_depth_usd,
        "big_bids": big_bids,
        "big_asks": big_asks,
    }
