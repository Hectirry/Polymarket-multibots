"""
feeds/binance_feed.py — Binance WebSocket price feed (SOLE external price source).

Connects to wss://stream.binance.com:9443/ws using a combined stream for all
configured symbols.  On disconnect, uses exponential back-off (max 5 retries).
Prices are cached in-memory; callers use get_price(symbol) at any time.

No Hyperliquid, no other price sources — Binance only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

from core.interfaces import PriceFeed
from core.models import PriceSnapshot

logger = logging.getLogger(__name__)

SUPPORTED_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "MATIC", "ARB", "OP"]
_BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"
_MAX_RETRIES = 5
_BACKOFF_BASE = 2.0  # seconds


class BinanceFeed(PriceFeed):
    """Streams miniTicker data for crypto/USDT pairs from Binance."""

    def __init__(self, symbols: Optional[list[str]] = None) -> None:
        self._symbols: list[str] = symbols or SUPPORTED_SYMBOLS
        self._prices: dict[str, PriceSnapshot] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ── PriceFeed interface ──────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="binance_ws")
        logger.info("BinanceFeed started for symbols: %s", self._symbols)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("BinanceFeed stopped")

    def get_price(self, symbol: str) -> Optional[PriceSnapshot]:
        return self._prices.get(symbol.upper())

    # ── Internal ─────────────────────────────────────────────────────────────

    def _build_ws_url(self) -> str:
        streams = "/".join(f"{s.lower()}usdt@miniTicker" for s in self._symbols)
        return f"{_BINANCE_WS_BASE}/{streams}"

    async def _run_loop(self) -> None:
        retries = 0
        while self._running and retries <= _MAX_RETRIES:
            url = self._build_ws_url()
            try:
                logger.info("Connecting to Binance WS: %s", url)
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    retries = 0  # reset on successful connect
                    await self._consume(ws)
            except (ConnectionClosedError, WebSocketException, OSError) as exc:
                retries += 1
                delay = _BACKOFF_BASE ** retries
                logger.warning(
                    "Binance WS disconnected (%s). Retry %d/%d in %.1fs",
                    exc, retries, _MAX_RETRIES, delay,
                )
                if retries > _MAX_RETRIES:
                    logger.error("Binance WS max retries exceeded — giving up")
                    break
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _consume(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            if not self._running:
                break
            try:
                msg = json.loads(raw)
                # Combined stream wraps in {"stream": ..., "data": {...}}
                data = msg.get("data", msg)
                self._handle_ticker(data)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.debug("Binance parse error: %s | raw=%s", exc, raw[:200])

    def _handle_ticker(self, data: dict) -> None:
        """Parse a miniTicker event and update the price cache."""
        symbol_pair: str = data.get("s", "")  # e.g. "BTCUSDT"
        if not symbol_pair.endswith("USDT"):
            return
        base = symbol_pair[: -len("USDT")]
        if base not in [s.upper() for s in self._symbols]:
            return
        try:
            price = float(data["c"])  # close price
        except (KeyError, ValueError):
            return

        self._prices[base] = PriceSnapshot(
            symbol=base,
            price=price,
            timestamp_ms=int(data.get("E", time.time() * 1000)),
            source="binance",
        )
        logger.debug("BinanceFeed updated %s = %.4f", base, price)


# ── Mock for dry-run / tests ──────────────────────────────────────────────────

class MockBinanceFeed(PriceFeed):
    """Returns static prices — used in --dry-run mode and tests."""

    _MOCK_PRICES = {
        "BTC": 65_000.0,
        "ETH": 3_200.0,
        "SOL": 165.0,
        "BNB": 580.0,
        "MATIC": 0.72,
        "ARB": 1.05,
        "OP": 2.40,
    }

    def __init__(self, overrides: Optional[dict[str, float]] = None) -> None:
        prices = {**self._MOCK_PRICES, **(overrides or {})}
        now_ms = int(time.time() * 1000)
        self._prices = {
            sym: PriceSnapshot(symbol=sym, price=p, timestamp_ms=now_ms, source="binance")
            for sym, p in prices.items()
        }

    async def start(self) -> None:
        logger.info("MockBinanceFeed started (dry-run)")

    async def stop(self) -> None:
        pass

    def get_price(self, symbol: str) -> Optional[PriceSnapshot]:
        return self._prices.get(symbol.upper())
