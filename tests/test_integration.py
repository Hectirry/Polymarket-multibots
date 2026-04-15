"""
tests/test_integration.py — End-to-end pipeline tests using mocks.

No real network calls are made.  Uses mock feeds and a temporary SQLite DB.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from core import database
from core.config import AppConfig
from core.models import Side, WhaleSignal
from engine.orderbook_analyzer import MockOrderbookAnalyzer
from engine.signal_router import SignalRouter
from feeds.binance_feed import MockBinanceFeed
from feeds.polymarket_feed import MockPolymarketFeed
from intelligence.whale_detector import MockWhaleDetector


@pytest.fixture(autouse=True)
async def tmp_db(tmp_path):
    """Each test gets a fresh in-memory-style DB."""
    db_path = str(tmp_path / "test.db")
    await database.init_db(db_path)
    yield
    await database.close_db()


# ── test_full_pipeline_dry_run ────────────────────────────────────────────────

class TestFullPipelineDryRun:
    @pytest.mark.asyncio
    async def test_pipeline_generates_signal_for_btc(self):
        """BTC market with strong spot > strike should generate a YES signal."""
        cfg = AppConfig(
            min_delta=0.10,
            min_ev_threshold=0.04,
            min_volume_24h=5_000,
            min_open_interest=1_000,
            max_spread=0.05,
            llm_validation_enabled=False,
            bankroll_usd=1_000.0,
            min_fill_depth_score=0.10,
            partial_fill_depth_threshold=0.25,
        )
        binance = MockBinanceFeed({"BTC": 75_000.0})  # above $70k strike
        poly = MockPolymarketFeed()
        analyzer = MockOrderbookAnalyzer()

        router = SignalRouter(cfg, analyzer, bankroll=1_000.0)

        markets = poly.get_active_markets()
        btc_market = next((m for m in markets if m.crypto_symbol == "BTC"), None)
        assert btc_market is not None

        price = binance.get_price("BTC")
        assert price is not None
        assert price.price == 75_000.0

        signal = await router.evaluate(btc_market, price, whale=None)

        # BTC at 75k vs $70k strike → binance_prob > 0.5 > market_prob=0.42
        # delta should be positive and meaningful
        assert signal is not None
        assert signal.side == Side.YES
        assert signal.delta > cfg.min_delta
        assert signal.size_usd > 0

    @pytest.mark.asyncio
    async def test_pipeline_rejects_when_spread_too_wide(self):
        """Markets with spread > max_spread must be rejected at liquidity filter."""
        cfg = AppConfig(max_spread=0.02)
        binance = MockBinanceFeed({"ETH": 4_000.0})
        poly = MockPolymarketFeed()
        analyzer = MockOrderbookAnalyzer()

        router = SignalRouter(cfg, analyzer, bankroll=1_000.0)

        markets = poly.get_active_markets()
        # All mock markets have spread=0.03 > 0.02 threshold
        eth_market = next((m for m in markets if m.crypto_symbol == "ETH"), None)
        assert eth_market is not None
        assert eth_market.spread > cfg.max_spread

        price = binance.get_price("ETH")
        signal = await router.evaluate(eth_market, price)

        assert signal is None

    @pytest.mark.asyncio
    async def test_pipeline_with_whale_boost(self):
        """Whale signal should boost delta and potentially enable a trade."""
        cfg = AppConfig(
            min_delta=0.05,   # low so whale adjustment can push over
            min_ev_threshold=0.02,
            llm_validation_enabled=False,
        )
        from core.models import MarketSnapshot

        # Market where spot barely > strike but whale pushes it over
        market = MarketSnapshot(
            condition_id="0xwhale001",
            token_id="tok_whale",
            question="Will ETH exceed $3,500 by end of April?",
            crypto_symbol="ETH",
            implied_prob=0.48,
            best_bid=0.48,
            best_ask=0.50,
            spread=0.02,
            volume_24h=60_000,
            open_interest=30_000,
            time_to_expiry_s=345_600,
            fraction_of_life_elapsed=0.4,
        )
        price_snap = MockBinanceFeed({"ETH": 3_600.0}).get_price("ETH")  # above $3500 strike
        whale = WhaleSignal(
            condition_id="0xwhale001",
            direction=Side.YES,
            total_volume_usd=2_000.0,
            trade_count=4,
            avg_price=0.50,
            pressure_score=0.80,
        )

        analyzer = MockOrderbookAnalyzer()
        router = SignalRouter(cfg, analyzer, bankroll=1_000.0)

        signal = await router.evaluate(market, price_snap, whale=whale)

        assert signal is not None
        assert signal.whale_score == pytest.approx(0.80)


# ── test_whale_detector_filters_small_trades ─────────────────────────────────

class TestWhaleDetectorFiltersSmallTrades:
    def test_mock_whale_detector_returns_configured_signal(self):
        """MockWhaleDetector returns exactly the pre-configured signal."""
        expected = WhaleSignal(
            condition_id="0xtest001",
            direction=Side.YES,
            total_volume_usd=5_000.0,
            trade_count=6,
            avg_price=0.45,
            pressure_score=0.85,
        )
        detector = MockWhaleDetector(signals={"0xtest001": expected})

        result = detector.get_signal("0xtest001")
        assert result is expected

        missing = detector.get_signal("0xnonexistent")
        assert missing is None

    @pytest.mark.asyncio
    async def test_real_detector_ignores_small_trades(self):
        """WhaleDetector._parse_trade filters trades below threshold."""
        from core.config import AppConfig
        from intelligence.whale_detector import WhaleDetector

        cfg = AppConfig(whale_min_trade_usd=500.0)
        detector = WhaleDetector(cfg)

        small_trade = {
            "id": "trade-small-001",
            "condition_id": "0xtest002",
            "outcome": "YES",
            "size": 100.0,   # below $500 threshold
            "price": 0.50,
            "timestamp": time.time(),
        }
        result = detector._parse_trade(small_trade)
        assert result is None

    @pytest.mark.asyncio
    async def test_real_detector_accepts_whale_trades(self):
        from core.config import AppConfig
        from intelligence.whale_detector import WhaleDetector

        cfg = AppConfig(whale_min_trade_usd=500.0)
        detector = WhaleDetector(cfg)

        whale_trade = {
            "id": "trade-whale-002",
            "condition_id": "0xtest003",
            "outcome": "YES",
            "size": 2_500.0,   # above threshold
            "price": 0.42,
            "timestamp": time.time(),
        }
        result = detector._parse_trade(whale_trade)
        assert result is not None
        assert result.size_usd == 2_500.0
        assert result.side == Side.YES

    @pytest.mark.asyncio
    async def test_whale_signal_requires_min_count(self):
        """WhaleSignal should be None if fewer than min_count trades in one direction."""
        from core.config import AppConfig
        from core.models import WhaleEvent
        from intelligence.whale_detector import WhaleDetector

        cfg = AppConfig(whale_min_count_for_signal=3, whale_pressure_window_s=300)
        detector = WhaleDetector(cfg)

        # Only 2 YES whale events — below threshold of 3
        now = time.time()
        detector._events["0xcid001"] = [
            WhaleEvent("t1", "0xcid001", Side.YES, 1_000.0, 0.45, now),
            WhaleEvent("t2", "0xcid001", Side.YES, 800.0, 0.46, now),
        ]

        signal = detector.get_signal("0xcid001")
        assert signal is None
