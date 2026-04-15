"""
intelligence/whale_detector.py — Polymarket whale trade detection.

Polls GET https://clob.polymarket.com/activity?limit=100&type=TRADE every 15s.
Filters trades >= whale_min_trade_usd (default $500) and aggregates per market.

WhaleSignal is emitted when 3+ large trades occur in the same direction within
the last 5 minutes for the same condition_id.

Deduplication: trade_id set prevents double-counting across polls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional

import aiohttp

from core.config import AppConfig
from core.interfaces import WhaleDetectorInterface
from core.models import Side, WhaleEvent, WhaleSignal

logger = logging.getLogger(__name__)

_ACTIVITY_URL = "https://data-api.polymarket.com/trades"


class WhaleDetector(WhaleDetectorInterface):
    """Polls Polymarket activity feed and surfaces whale pressure signals."""

    def __init__(self, cfg: AppConfig, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._cfg = cfg
        self._session = session
        self._own_session = session is None
        self._seen_trade_ids: set[str] = set()
        # condition_id → list of WhaleEvent (within pressure window)
        self._events: dict[str, list[WhaleEvent]] = defaultdict(list)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ── WhaleDetectorInterface ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._own_session:
            self._session = aiohttp.ClientSession()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="whale_detector")
        logger.info("WhaleDetector started (threshold=$%.0f)", self._cfg.whale_min_trade_usd)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._own_session and self._session:
            await self._session.close()
        logger.info("WhaleDetector stopped")

    def get_signal(self, condition_id: str) -> Optional[WhaleSignal]:
        """Return aggregated whale signal for a market, or None."""
        self._expire_old_events()
        events = self._events.get(condition_id, [])
        if not events:
            return None
        return self._aggregate(condition_id, events)

    def get_recent_events(self, limit: int = 50) -> list[WhaleEvent]:
        """Return most recent whale events across all markets."""
        all_events = [e for events in self._events.values() for e in events]
        all_events.sort(key=lambda e: e.timestamp, reverse=True)
        return all_events[:limit]

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch_and_process()
            except Exception as exc:
                logger.warning("WhaleDetector poll error: %s", exc)
            await asyncio.sleep(self._cfg.whale_poll_interval_s)

    async def _fetch_and_process(self) -> None:
        assert self._session is not None
        params = {"limit": 100}
        async with self._session.get(
            _ACTIVITY_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        trades = data if isinstance(data, list) else data.get("data", [])
        new_count = 0
        for trade in trades:
            event = self._parse_trade(trade)
            if event is None:
                continue
            if event.trade_id in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(event.trade_id)
            self._events[event.condition_id].append(event)
            new_count += 1

        if new_count:
            logger.debug("WhaleDetector: %d new whale events", new_count)

        self._expire_old_events()
        # Keep seen_trade_ids bounded
        if len(self._seen_trade_ids) > 10_000:
            self._seen_trade_ids = set(list(self._seen_trade_ids)[-5_000:])

    def _parse_trade(self, raw: dict) -> Optional[WhaleEvent]:
        """Convert a raw activity record to a WhaleEvent, or None if filtered.

        Handles both the legacy CLOB activity format (snake_case) and the
        data-api.polymarket.com/trades format (camelCase).
        """
        try:
            # data-api: size is in shares; USD value = size * price
            # legacy CLOB: size_usd is already USD
            price = float(raw.get("price", 0.5) or 0.5)
            size_shares = float(raw.get("size", 0) or 0)
            size_usd_direct = float(raw.get("size_usd", 0) or 0)
            size_usd = size_usd_direct if size_usd_direct else size_shares * price

            if size_usd < self._cfg.whale_min_trade_usd:
                return None

            # trade_id: prefer transactionHash (data-api), fall back to id/trade_id
            trade_id = str(
                raw.get("transactionHash") or raw.get("id") or raw.get("trade_id", "")
            )
            if not trade_id:
                return None

            # condition_id: camelCase (data-api) or snake_case (CLOB)
            condition_id = str(
                raw.get("conditionId") or raw.get("condition_id") or raw.get("market_id", "")
            )
            if not condition_id:
                return None

            # outcome: "Up"/"Down" (data-api crypto), "YES"/"NO", or "BUY"/"SELL"
            outcome = str(raw.get("outcome") or raw.get("side", "YES")).upper()
            side = Side.YES if outcome in ("YES", "UP", "BUY") else Side.NO

            ts_raw = raw.get("timestamp") or raw.get("created_at") or time.time()
            if isinstance(ts_raw, str):
                from datetime import datetime, timezone
                ts = datetime.fromisoformat(ts_raw.rstrip("Z")).replace(
                    tzinfo=timezone.utc
                ).timestamp()
            else:
                ts = float(ts_raw)

            return WhaleEvent(
                trade_id=trade_id,
                condition_id=condition_id,
                side=side,
                size_usd=size_usd,
                price=price,
                timestamp=ts,
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Failed to parse whale trade: %s | raw=%s", exc, str(raw)[:100])
            return None

    def _expire_old_events(self) -> None:
        """Remove events outside the pressure window."""
        cutoff = time.time() - self._cfg.whale_pressure_window_s
        for cid in list(self._events.keys()):
            self._events[cid] = [e for e in self._events[cid] if e.timestamp >= cutoff]
            if not self._events[cid]:
                del self._events[cid]

    def _aggregate(self, condition_id: str, events: list[WhaleEvent]) -> Optional[WhaleSignal]:
        """Aggregate events into a WhaleSignal if threshold is met."""
        yes_events = [e for e in events if e.side == Side.YES]
        no_events = [e for e in events if e.side == Side.NO]

        yes_vol = sum(e.size_usd for e in yes_events)
        no_vol = sum(e.size_usd for e in no_events)

        if yes_vol >= no_vol:
            dominant_events = yes_events
            direction = Side.YES
            total_vol = yes_vol
        else:
            dominant_events = no_events
            direction = Side.NO
            total_vol = no_vol

        min_count = self._cfg.whale_min_count_for_signal
        if len(dominant_events) < min_count:
            return None  # not enough trades in one direction

        avg_price = sum(e.price for e in dominant_events) / len(dominant_events)
        all_vol = yes_vol + no_vol
        pressure_score = total_vol / all_vol if all_vol > 0 else 0.5
        # Also factor in count dominance
        count_ratio = len(dominant_events) / max(len(events), 1)
        pressure_score = (pressure_score + count_ratio) / 2.0
        pressure_score = min(1.0, max(0.0, pressure_score))

        logger.debug(
            "WhaleSignal %s: dir=%s vol=$%.0f count=%d pressure=%.2f",
            condition_id, direction, total_vol, len(dominant_events), pressure_score,
        )
        return WhaleSignal(
            condition_id=condition_id,
            direction=direction,
            total_volume_usd=total_vol,
            trade_count=len(dominant_events),
            avg_price=avg_price,
            pressure_score=pressure_score,
            timestamp=time.time(),
        )


# ── Mock for dry-run / tests ──────────────────────────────────────────────────

class MockWhaleDetector(WhaleDetectorInterface):
    """Returns pre-configured signals — used in --dry-run and tests."""

    def __init__(self, signals: Optional[dict[str, WhaleSignal]] = None) -> None:
        self._signals = signals or {}

    async def start(self) -> None:
        logger.info("MockWhaleDetector started")

    async def stop(self) -> None:
        pass

    def get_signal(self, condition_id: str) -> Optional[WhaleSignal]:
        return self._signals.get(condition_id)

    def get_recent_events(self, limit: int = 50) -> list[WhaleEvent]:
        return []
