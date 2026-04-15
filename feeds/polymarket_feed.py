"""
feeds/polymarket_feed.py — Polymarket market discovery + price streaming.

Responsibilities:
  1. Discover active crypto markets via REST (rescan every 60 s).
  2. Stream price updates via WebSocket (wss://ws-subscriptions-clob.polymarket.com/ws/market).
  3. Fall back to REST polling every 10 s if WS is unavailable.
  4. Expose get_active_markets() returning a list[MarketSnapshot].

Only markets passing liquidity/timing/spread filters are retained.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

import aiohttp
import websockets
from websockets.exceptions import WebSocketException

from core.config import AppConfig
from core.interfaces import MarketFeed
from core.models import MarketSnapshot

logger = logging.getLogger(__name__)

_CLOB_BASE = "https://clob.polymarket.com"
_GAMMA_BASE = "https://gamma-api.polymarket.com"
_DATA_API_BASE = "https://data-api.polymarket.com"
_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Symbols we watch — must match BinanceFeed symbols
_CRYPTO_KEYWORDS = ["BTC", "ETH", "SOL", "BNB", "MATIC", "ARB", "OP"]
_SYMBOL_RE = re.compile(r"\b(" + "|".join(_CRYPTO_KEYWORDS) + r")\b")

# Full names used in Polymarket event titles (e.g. "Bitcoin above $X on April 15?")
_CRYPTO_FULL_NAMES = {
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH",
    "SOLANA": "SOL",
    "BINANCE": "BNB",
    "MATIC": "MATIC",
    "ARBITRUM": "ARB",
    "OPTIMISM": "OP",
}
_FULL_NAME_RE = re.compile(
    r"\b(" + "|".join(_CRYPTO_FULL_NAMES.keys()) + r")\b", re.IGNORECASE
)


def _extract_crypto_symbol(question: str) -> Optional[str]:
    """Extract a tracked crypto symbol from a market question.

    Matches both abbreviations (BTC, ETH) and full names (Bitcoin, Ethereum).
    """
    m = _SYMBOL_RE.search(question.upper())
    if m:
        return m.group(1)
    # Try full names (case-insensitive)
    m2 = _FULL_NAME_RE.search(question)
    if m2:
        return _CRYPTO_FULL_NAMES.get(m2.group(1).upper())
    return None


def _compute_fraction_elapsed(market: dict) -> float:
    """Estimate what fraction of market life has elapsed."""
    start = market.get("start_date_iso", "") or ""
    end = market.get("end_date_iso", "") or ""
    if not start or not end:
        return 0.5  # unknown — assume midpoint
    try:
        from datetime import datetime, timezone
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        def _parse(s: str) -> float:
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                return datetime.fromisoformat(s.rstrip("Z")).replace(
                    tzinfo=timezone.utc
                ).timestamp()

        t_start = _parse(start)
        t_end = _parse(end)
        duration = t_end - t_start
        if duration <= 0:
            return 0.5
        elapsed = time.time() - t_start
        return max(0.0, min(1.0, elapsed / duration))
    except Exception:
        return 0.5


def _parse_market(raw: dict, cfg: AppConfig) -> Optional[MarketSnapshot]:
    """Validate and convert a raw market dict to MarketSnapshot.

    Handles both CLOB API (snake_case) and Gamma API (camelCase) formats.
    """
    question = raw.get("question", "")
    symbol = _extract_crypto_symbol(question)
    if not symbol:
        return None  # not a crypto market we track

    if raw.get("closed") or raw.get("resolved"):
        return None  # skip resolved/closed markets
    # Skip markets not currently accepting orders (Gamma API field)
    if raw.get("acceptingOrders") is False:
        return None

    # ── Condition ID ─────────────────────────────────────────────────────────
    condition_id = raw.get("condition_id") or raw.get("conditionId", "")
    if not condition_id:
        return None

    # ── Token ID (YES/Up outcome) ─────────────────────────────────────────────
    token_id = ""
    best_bid = 0.0

    # CLOB API format: "tokens" array with outcome+price per token
    tokens = raw.get("tokens", [])
    if tokens:
        yes_token = next(
            (t for t in tokens if t.get("outcome", "").upper() in ("YES", "UP")), None
        )
        if yes_token:
            token_id = yes_token.get("token_id", "")
            best_bid = float(yes_token.get("price", 0))

    # Gamma API format: "clobTokenIds" JSON string + "outcomePrices" JSON string
    if not token_id:
        clob_ids_raw = raw.get("clobTokenIds", "[]") or "[]"
        outcomes_raw = raw.get("outcomes", '["Yes","No"]') or '["Yes","No"]'
        prices_raw = raw.get("outcomePrices", '["0.5","0.5"]') or '["0.5","0.5"]'
        try:
            clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        except (json.JSONDecodeError, TypeError):
            clob_ids, outcomes, prices = [], ["Yes", "No"], ["0.5", "0.5"]

        yes_idx = next(
            (i for i, o in enumerate(outcomes) if str(o).upper() in ("YES", "UP")), 0
        )
        if clob_ids and yes_idx < len(clob_ids):
            token_id = str(clob_ids[yes_idx])
        if prices and yes_idx < len(prices):
            best_bid = float(prices[yes_idx])

    if not token_id:
        return None

    # ── Price / spread ────────────────────────────────────────────────────────
    # Use live book prices from Gamma API when available
    if raw.get("bestBid") is not None:
        best_bid = float(raw["bestBid"])
    if raw.get("bestAsk") is not None:
        best_ask = float(raw["bestAsk"])
    else:
        best_ask = min(best_bid + 0.02, 0.99)
    if raw.get("spread") is not None:
        spread = float(raw["spread"])
    else:
        spread = best_ask - best_bid

    # ── Volume / liquidity ────────────────────────────────────────────────────
    volume_24h = float(
        raw.get("volume_24hr") or raw.get("volume24hrClob") or raw.get("volume", 0) or 0
    )
    open_interest = float(
        raw.get("open_interest") or raw.get("liquidityClob") or 0
    )

    # ── Expiry ────────────────────────────────────────────────────────────────
    end_date = raw.get("endDate") or raw.get("end_date_iso", "")
    start_date = raw.get("startDate") or raw.get("start_date_iso", "")
    try:
        from datetime import datetime, timezone
        end_ts = datetime.fromisoformat(end_date.rstrip("Z")).replace(
            tzinfo=timezone.utc
        ).timestamp()
        time_to_expiry_s = end_ts - time.time()
    except Exception:
        time_to_expiry_s = 86_400

    # ── Config filters ────────────────────────────────────────────────────────
    if volume_24h < cfg.min_volume_24h:
        logger.debug("Skipping %s — volume_24h %.0f < %.0f", condition_id, volume_24h, cfg.min_volume_24h)
        return None
    if open_interest < cfg.min_open_interest:
        logger.debug("Skipping %s — open_interest %.0f < %.0f", condition_id, open_interest, cfg.min_open_interest)
        return None
    if not (cfg.min_time_remaining_s <= time_to_expiry_s <= cfg.max_time_remaining_s):
        logger.debug("Skipping %s — time_to_expiry %.0f out of range", condition_id, time_to_expiry_s)
        return None
    if spread > cfg.max_spread:
        logger.debug("Skipping %s — spread %.3f > %.3f", condition_id, spread, cfg.max_spread)
        return None

    # Build a synthetic start/end dict for fraction_elapsed compatible with helper
    fraction_raw = {
        "start_date_iso": start_date,
        "end_date_iso": end_date,
    }
    fraction = _compute_fraction_elapsed(fraction_raw)
    implied_prob = best_bid

    return MarketSnapshot(
        condition_id=condition_id,
        token_id=token_id,
        question=question,
        crypto_symbol=symbol,
        implied_prob=implied_prob,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        volume_24h=volume_24h,
        open_interest=open_interest,
        time_to_expiry_s=time_to_expiry_s,
        fraction_of_life_elapsed=fraction,
        resolved=bool(raw.get("resolved")),
        resolution_price=raw.get("resolution_price"),
        timestamp=time.time(),
    )


class PolymarketFeed(MarketFeed):
    """Discovers and streams Polymarket crypto market data."""

    def __init__(self, cfg: AppConfig, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._cfg = cfg
        self._session = session
        self._own_session = session is None
        self._markets: dict[str, MarketSnapshot] = {}   # condition_id → snapshot
        self._running = False
        self._tasks: list[asyncio.Task] = []

    # ── MarketFeed interface ─────────────────────────────────────────────────

    async def start(self) -> None:
        if self._own_session:
            self._session = aiohttp.ClientSession()
        self._running = True
        # Initial discovery (blocking so first snapshot is available immediately)
        await self._discover_markets()
        self._tasks.append(asyncio.create_task(self._rescan_loop(), name="pm_rescan"))
        self._tasks.append(asyncio.create_task(self._ws_loop(), name="pm_ws"))
        logger.info("PolymarketFeed started — %d markets", len(self._markets))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._own_session and self._session:
            await self._session.close()
        logger.info("PolymarketFeed stopped")

    def get_active_markets(self) -> list[MarketSnapshot]:
        return list(self._markets.values())

    # ── Discovery ────────────────────────────────────────────────────────────

    async def _discover_markets(self) -> None:
        """Discover active crypto markets via the Gamma API events endpoint.

        Fetches the top-volume events, filters for crypto keywords, then parses
        every constituent market that is still accepting orders.
        """
        assert self._session is not None

        raw_markets: list[dict] = []
        try:
            async with self._session.get(
                f"{_GAMMA_BASE}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 200,
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:
            logger.warning("Market discovery failed: %s", exc)
            return

        events = data if isinstance(data, list) else data.get("data", [])
        for event in events:
            title = event.get("title", "") or ""
            # Match both abbreviations (BTC) and full names (Bitcoin) in event titles
            if not _extract_crypto_symbol(title.upper()) and not _FULL_NAME_RE.search(title):
                continue
            for market in event.get("markets") or []:
                raw_markets.append(market)

        found = 0
        for raw in raw_markets:
            ms = _parse_market(raw, self._cfg)
            if ms:
                self._markets[ms.condition_id] = ms
                found += 1
        logger.info("Market scan complete — %d/%d passed filters", found, len(raw_markets))

    async def _rescan_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._cfg.market_rescan_interval_s)
            await self._discover_markets()

    # ── WebSocket streaming ──────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        """Subscribe to price_change + book_update for active markets via WS."""
        while self._running:
            try:
                async with websockets.connect(_WS_URL, ping_interval=30) as ws:
                    await self._subscribe(ws)
                    await self._consume_ws(ws)
            except (WebSocketException, OSError) as exc:
                logger.warning("Polymarket WS error: %s — falling back to REST polling", exc)
                await self._rest_poll_loop()

    async def _subscribe(self, ws) -> None:
        cids = list(self._markets.keys())
        if not cids:
            return
        payload = {
            "type": "subscribe",
            "channel": "market",
            "markets": cids,
        }
        await ws.send(json.dumps(payload))
        logger.info("Polymarket WS subscribed to %d markets", len(cids))

    async def _consume_ws(self, ws) -> None:
        async for raw in ws:
            if not self._running:
                break
            try:
                msg = json.loads(raw)
                self._apply_ws_update(msg)
            except (json.JSONDecodeError, KeyError):
                pass

    def _apply_ws_update(self, msg: dict) -> None:
        """Update the in-memory snapshot from a WS price_change or book_update."""
        event_type = msg.get("event_type", "")
        cid = msg.get("market", msg.get("condition_id", ""))
        if cid not in self._markets:
            return
        ms = self._markets[cid]
        if event_type in ("price_change", "book_update"):
            price = msg.get("price") or msg.get("best_bid")
            if price is not None:
                ms.implied_prob = float(price)
                ms.best_bid = float(price)
                ms.timestamp = time.time()
                ms.time_to_expiry_s = max(0.0, ms.time_to_expiry_s - 1)

    async def _rest_poll_loop(self) -> None:
        """Fallback: poll REST every 10s while WS is down."""
        logger.info("Polymarket REST fallback polling active")
        while self._running:
            await asyncio.sleep(10)
            try:
                await self._discover_markets()
            except Exception as exc:
                logger.debug("REST poll error: %s", exc)


# ── Mock for dry-run / tests ─────────────────────────────────────────────────

class MockPolymarketFeed(MarketFeed):
    """Returns synthetic markets — used in --dry-run and tests."""

    def __init__(self) -> None:
        self._markets: list[MarketSnapshot] = [
            MarketSnapshot(
                condition_id="0xabc001",
                token_id="tok_btc_001",
                question="Will BTC be above $70,000 on May 1?",
                crypto_symbol="BTC",
                implied_prob=0.42,
                best_bid=0.42,
                best_ask=0.45,
                spread=0.03,
                volume_24h=85_000,
                open_interest=42_000,
                time_to_expiry_s=432_000,
                fraction_of_life_elapsed=0.35,
            ),
            MarketSnapshot(
                condition_id="0xdef002",
                token_id="tok_eth_002",
                question="Will ETH surpass $4,000 by end of April?",
                crypto_symbol="ETH",
                implied_prob=0.31,
                best_bid=0.31,
                best_ask=0.34,
                spread=0.03,
                volume_24h=55_000,
                open_interest=28_000,
                time_to_expiry_s=259_200,
                fraction_of_life_elapsed=0.60,
            ),
            MarketSnapshot(
                condition_id="0xghi003",
                token_id="tok_sol_003",
                question="Will SOL reach $200 before April 30?",
                crypto_symbol="SOL",
                implied_prob=0.55,
                best_bid=0.55,
                best_ask=0.57,
                spread=0.02,
                volume_24h=32_000,
                open_interest=18_000,
                time_to_expiry_s=345_600,
                fraction_of_life_elapsed=0.25,
            ),
        ]

    async def start(self) -> None:
        logger.info("MockPolymarketFeed started (dry-run) — %d markets", len(self._markets))

    async def stop(self) -> None:
        pass

    def get_active_markets(self) -> list[MarketSnapshot]:
        return list(self._markets)
