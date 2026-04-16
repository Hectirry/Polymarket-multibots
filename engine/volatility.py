"""
engine/volatility.py — Realized volatility provider for Binance crypto pairs.

Fetches recent klines from Binance public REST and computes the annualized
standard deviation of log returns. Cached per symbol with a TTL so we don't
hammer the API during every market scan.

Used by `engine.probability` to compute Black-Scholes style P(spot > strike
at expiry) given the horizon, instead of the fixed-steepness logistic that
treated all time horizons the same.

On any fetch failure the provider returns None; probability.estimate then
falls back to the legacy logistic so signals still flow (neutrally).
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

import aiohttp

from core.config import AppConfig

logger = logging.getLogger(__name__)

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

_INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400,
}


class VolatilityProvider:
    """Computes annualized realized volatility for a crypto symbol."""

    def __init__(
        self,
        cfg: AppConfig,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._cfg = cfg
        self._session = session
        self._own_session = session is None
        # symbol → (ts_computed, annualized_sigma)
        self._cache: dict[str, tuple[float, float]] = {}

    async def start(self) -> None:
        if self._own_session:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._own_session and self._session:
            await self._session.close()
            self._session = None

    def _cached(self, symbol: str) -> Optional[float]:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        ts, sigma = entry
        if (time.time() - ts) >= self._cfg.vol_cache_ttl_s:
            return None
        return sigma

    async def get(self, symbol: str) -> Optional[float]:
        """Return annualized sigma (e.g. 0.60 = 60%) or None on failure."""
        cached = self._cached(symbol)
        if cached is not None:
            return cached

        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True

        pair = f"{symbol.upper()}USDT"
        interval = self._cfg.vol_kline_interval
        limit = self._cfg.vol_kline_count

        try:
            params = {"symbol": pair, "interval": interval, "limit": limit}
            async with self._session.get(
                _BINANCE_KLINES_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                resp.raise_for_status()
                rows = await resp.json()
        except Exception as exc:
            logger.warning(
                "VolatilityProvider fetch failed for %s (%s) — returning None",
                symbol, exc,
            )
            return None

        # Binance kline row: [open_time, open, high, low, close, volume, ...]
        closes = [float(r[4]) for r in rows if r and len(r) >= 5]
        if len(closes) < 3:
            logger.warning("VolatilityProvider: only %d closes for %s", len(closes), symbol)
            return None

        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        n = len(log_returns)
        if n < 2:
            return None

        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        sigma_per_bar = math.sqrt(variance)

        interval_s = _INTERVAL_SECONDS.get(interval, 3600)
        bars_per_year = (365.25 * 24 * 3600) / interval_s
        sigma_annual = sigma_per_bar * math.sqrt(bars_per_year)

        self._cache[symbol] = (time.time(), sigma_annual)
        logger.info(
            "VolatilityProvider %s: σ_annual=%.1f%% (from %d %s bars)",
            symbol, sigma_annual * 100, n, interval,
        )
        return sigma_annual
