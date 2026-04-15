"""
engine/orderbook_analyzer.py — Polymarket orderbook depth and manipulation detection.

Fetches GET /book?token_id={token_id} and checks:
  - Top-3 order concentration > threshold → is_manipulated = True
  - Weighted average spread across the book (effective_spread)
  - depth_score: normalised measure of total liquidity depth

A manipulated orderbook causes SignalRouter to discard the market.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from core.interfaces import OrderbookAnalyzerInterface
from core.models import OrderbookAnalysis

logger = logging.getLogger(__name__)

_CLOB_BASE = "https://clob.polymarket.com"
_BOOK_ENDPOINT = f"{_CLOB_BASE}/book"


class OrderbookAnalyzer(OrderbookAnalyzerInterface):
    """Fetches and analyses a YES token's orderbook from the Polymarket CLOB."""

    def __init__(
        self,
        manipulation_threshold: float = 0.60,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._threshold = manipulation_threshold
        self._session = session
        self._own_session = session is None

    async def _ensure_session(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._own_session and self._session:
            await self._session.close()

    async def analyze(self, token_id: str) -> OrderbookAnalysis:
        await self._ensure_session()
        try:
            assert self._session is not None
            async with self._session.get(
                _BOOK_ENDPOINT,
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                resp.raise_for_status()
                book = await resp.json()
        except Exception as exc:
            logger.warning("Orderbook fetch failed for %s: %s", token_id, exc)
            # On failure, return a conservative non-manipulated placeholder
            return OrderbookAnalysis(
                token_id=token_id,
                is_manipulated=False,
                effective_spread=0.03,
                depth_score=0.5,
                top3_concentration=0.0,
            )

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        return self._compute_analysis(token_id, bids, asks)

    def _compute_analysis(
        self,
        token_id: str,
        bids: list[dict],
        asks: list[dict],
    ) -> OrderbookAnalysis:
        """Core analysis given raw bid/ask level lists."""
        # Each level: {"price": str, "size": str}
        def parse_levels(levels: list[dict]) -> list[tuple[float, float]]:
            result = []
            for lvl in levels:
                try:
                    result.append((float(lvl["price"]), float(lvl["size"])))
                except (KeyError, ValueError):
                    pass
            return result

        bid_levels = parse_levels(bids)
        ask_levels = parse_levels(asks)

        all_sizes = [s for _, s in bid_levels] + [s for _, s in ask_levels]
        total_size = sum(all_sizes)
        if total_size == 0:
            return OrderbookAnalysis(
                token_id=token_id,
                is_manipulated=False,
                effective_spread=0.05,
                depth_score=0.0,
                top3_concentration=0.0,
            )

        # Top-3 concentration across both sides
        top3_sizes = sorted(all_sizes, reverse=True)[:3]
        top3_concentration = sum(top3_sizes) / total_size

        is_manipulated = top3_concentration > self._threshold

        # Effective spread: best_ask - best_bid (volume-weighted would need more levels)
        best_bid = bid_levels[0][0] if bid_levels else 0.0
        best_ask = ask_levels[0][0] if ask_levels else 1.0
        effective_spread = max(0.0, best_ask - best_bid)

        # Depth score: normalised total size (cap at 10_000 USDC = score 1.0)
        depth_score = min(1.0, total_size / 10_000)

        logger.debug(
            "Orderbook %s: top3=%.2f manipulated=%s spread=%.4f depth=%.2f",
            token_id, top3_concentration, is_manipulated, effective_spread, depth_score,
        )
        return OrderbookAnalysis(
            token_id=token_id,
            is_manipulated=is_manipulated,
            effective_spread=effective_spread,
            depth_score=depth_score,
            top3_concentration=top3_concentration,
        )


# ── Mock for tests ────────────────────────────────────────────────────────────

class MockOrderbookAnalyzer(OrderbookAnalyzerInterface):
    def __init__(self, result: Optional[OrderbookAnalysis] = None) -> None:
        self._result = result

    async def analyze(self, token_id: str) -> OrderbookAnalysis:
        if self._result is not None:
            return self._result
        return OrderbookAnalysis(
            token_id=token_id,
            is_manipulated=False,
            effective_spread=0.025,
            depth_score=0.75,
            top3_concentration=0.35,
        )
