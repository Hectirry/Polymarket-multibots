"""
core/interfaces.py — Abstract base classes defining contracts between modules.

Concrete implementations live in feeds/, engine/, execution/, intelligence/.
Using ABCs keeps the signal_router testable without real network calls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.models import (
    MarketSnapshot,
    OrderbookAnalysis,
    PriceSnapshot,
    Signal,
    Trade,
    WhaleSignal,
)


class PriceFeed(ABC):
    """Source of spot crypto prices (Binance)."""

    @abstractmethod
    async def start(self) -> None:
        """Begin streaming prices."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down."""

    @abstractmethod
    def get_price(self, symbol: str) -> Optional[PriceSnapshot]:
        """Return the latest cached price for symbol, or None if unavailable."""


class MarketFeed(ABC):
    """Source of Polymarket market snapshots."""

    @abstractmethod
    async def start(self) -> None:
        """Begin discovery and streaming."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down."""

    @abstractmethod
    def get_active_markets(self) -> list[MarketSnapshot]:
        """Return all currently tracked market snapshots."""


class WhaleDetectorInterface(ABC):
    """Detects and aggregates whale activity on Polymarket."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def get_signal(self, condition_id: str) -> Optional[WhaleSignal]:
        """Return the latest whale signal for a market, or None."""

    @abstractmethod
    def get_recent_events(self, limit: int = 50) -> list:
        """Return recent raw whale events for display."""


class OrderbookAnalyzerInterface(ABC):
    """Analyses Polymarket orderbook depth and manipulation."""

    @abstractmethod
    async def analyze(self, token_id: str) -> OrderbookAnalysis: ...


class OrderExecutorInterface(ABC):
    """Submits or simulates trade orders."""

    @abstractmethod
    async def execute(self, signal: Signal) -> Trade: ...

    @abstractmethod
    async def close_trade(self, trade: Trade, exit_price: float) -> Trade: ...
