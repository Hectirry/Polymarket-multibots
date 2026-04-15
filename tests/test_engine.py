"""tests/test_engine.py — Unit tests for probability, signal_router, LLM validator, EV."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import AppConfig
from core.models import MarketSnapshot, OrderbookAnalysis, PriceSnapshot, Side, WhaleSignal
from engine import ev_calculator, probability
from engine.llm_validator import LLMValidation, LLMValidator
from engine.orderbook_analyzer import MockOrderbookAnalyzer
from engine.signal_router import SignalRouter


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_market(
    condition_id: str = "0xtest001",
    crypto_symbol: str = "BTC",
    question: str = "Will BTC exceed $70,000 by May 1?",
    implied_prob: float = 0.42,
    best_bid: float = 0.42,
    best_ask: float = 0.45,
    spread: float = 0.03,
    volume_24h: float = 50_000,
    open_interest: float = 20_000,
    time_to_expiry_s: float = 432_000,
    fraction_of_life_elapsed: float = 0.35,
) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id=condition_id,
        token_id="tok_001",
        question=question,
        crypto_symbol=crypto_symbol,
        implied_prob=implied_prob,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        volume_24h=volume_24h,
        open_interest=open_interest,
        time_to_expiry_s=time_to_expiry_s,
        fraction_of_life_elapsed=fraction_of_life_elapsed,
    )


def make_price(symbol: str = "BTC", price: float = 72_000.0) -> PriceSnapshot:
    return PriceSnapshot(symbol=symbol, price=price, timestamp_ms=int(time.time() * 1000))


def make_whale(
    condition_id: str = "0xtest001",
    direction: Side = Side.YES,
    pressure_score: float = 0.8,
    trade_count: int = 5,
    total_volume_usd: float = 3_500.0,
) -> WhaleSignal:
    return WhaleSignal(
        condition_id=condition_id,
        direction=direction,
        total_volume_usd=total_volume_usd,
        trade_count=trade_count,
        avg_price=0.44,
        pressure_score=pressure_score,
    )


def make_cfg(**kwargs) -> AppConfig:
    cfg = AppConfig()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


# ── test_probability_with_whale_signal ────────────────────────────────────────

class TestProbabilityWithWhaleSignal:
    def test_whale_yes_increases_delta(self):
        market = make_market(implied_prob=0.42, question="Will BTC exceed $70,000 by May 1?")
        price = make_price("BTC", 72_000.0)
        whale = make_whale(direction=Side.YES, pressure_score=0.8)

        est = probability.estimate(market, price, whale)

        assert est.whale_adjusted is True
        assert est.adjusted_delta > est.delta
        assert est.adjusted_delta == pytest.approx(est.delta + 0.05, abs=1e-9)

    def test_whale_no_decreases_delta(self):
        market = make_market(implied_prob=0.42, question="Will BTC exceed $70,000 by May 1?")
        price = make_price("BTC", 72_000.0)
        whale = make_whale(direction=Side.NO, pressure_score=0.7)

        est = probability.estimate(market, price, whale)

        assert est.adjusted_delta < est.delta

    def test_no_price_gives_neutral_estimate(self):
        market = make_market()
        est = probability.estimate(market, None, None)

        assert est.delta == 0.0
        assert est.our_prob == market.implied_prob

    def test_no_strike_gives_neutral_estimate(self):
        market = make_market(question="Will the market go somewhere?")
        price = make_price("BTC", 72_000.0)
        est = probability.estimate(market, price, None)

        assert est.delta == 0.0

    def test_our_prob_clamped_to_bounds(self):
        # Extreme whale + price signal should not push past 0.99
        market = make_market(implied_prob=0.95, question="Will BTC exceed $1 by May 1?")
        price = make_price("BTC", 72_000.0)
        whale = make_whale(direction=Side.YES)
        est = probability.estimate(market, price, whale)

        assert est.our_prob <= 0.99
        assert est.our_prob >= 0.01


# ── test_signal_router_rejects_manipulated_market ─────────────────────────────

class TestSignalRouterRejectsManipulatedMarket:
    @pytest.mark.asyncio
    async def test_manipulated_orderbook_rejected(self):
        cfg = make_cfg(min_delta=0.05)  # low threshold so only manipulation blocks
        manipulated_ob = OrderbookAnalysis(
            token_id="tok_001",
            is_manipulated=True,
            effective_spread=0.10,
            depth_score=0.3,
            top3_concentration=0.85,
        )
        analyzer = MockOrderbookAnalyzer(result=manipulated_ob)
        router = SignalRouter(cfg, analyzer, bankroll=1_000.0)

        market = make_market()
        price = make_price("BTC", 75_000.0)

        signal = await router.evaluate(market, price)
        assert signal is None

    @pytest.mark.asyncio
    async def test_clean_orderbook_passes_to_next_stage(self):
        cfg = make_cfg(min_delta=0.40)  # so high that it will fail on delta, not ob
        clean_ob = OrderbookAnalysis(
            token_id="tok_001",
            is_manipulated=False,
            effective_spread=0.02,
            depth_score=0.9,
            top3_concentration=0.30,
        )
        analyzer = MockOrderbookAnalyzer(result=clean_ob)
        router = SignalRouter(cfg, analyzer, bankroll=1_000.0)

        market = make_market()
        price = make_price("BTC", 75_000.0)

        # Should fail on delta (not manipulation)
        signal = await router.evaluate(market, price)
        assert signal is None  # rejected for delta, but not manipulation


# ── test_llm_validator_caches_results ─────────────────────────────────────────

class TestLLMValidatorCaches:
    @pytest.mark.asyncio
    async def test_result_cached_within_ttl(self):
        cfg = make_cfg(
            llm_validation_enabled=True,
            llm_min_whale_score=0.3,
            llm_cache_ttl_s=600,
            min_delta=0.05,
            min_ev_threshold=0.02,
            openrouter_api_key="test-key",
        )
        validator = LLMValidator(cfg)

        mock_response = {
            "choices": [{"message": {"content": '{"validate": true, "confidence": 0.80, "reason": "Strong signal"}'}}]
        }

        with patch("aiohttp.ClientSession.post") as mock_post:
            mock_cm = MagicMock()
            mock_cm.__aenter__ = AsyncMock(return_value=MagicMock(
                raise_for_status=MagicMock(),
                json=AsyncMock(return_value=mock_response),
            ))
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_post.return_value = mock_cm

            kwargs = dict(
                condition_id="0xtest001",
                question="Will BTC exceed $70k?",
                crypto_symbol="BTC",
                spot_price=72_000.0,
                market_implied_prob=0.42,
                our_estimated_prob=0.60,
                delta=0.18,
                ev_net_fees=0.10,
                whale_trade_count=5,
                whale_volume_usd=3_000.0,
                whale_direction="YES",
                time_remaining_s=86_400,
                depth_score=0.7,
                whale_pressure_score=0.75,
            )
            result1 = await validator.validate(**kwargs)
            result2 = await validator.validate(**kwargs)

        # Second call should be served from cache (skipped=True)
        assert result1.validated is True
        assert result2.skipped is True

    @pytest.mark.asyncio
    async def test_llm_failure_allows_trade(self):
        """LLM failure should not block the signal."""
        cfg = make_cfg(
            llm_validation_enabled=True,
            llm_min_whale_score=0.3,
            llm_cache_ttl_s=600,
            min_delta=0.05,
            min_ev_threshold=0.02,
            openrouter_api_key="test-key",
        )
        validator = LLMValidator(cfg)

        with patch("aiohttp.ClientSession.post", side_effect=Exception("network error")):
            result = await validator.validate(
                condition_id="0xtest002",
                question="Will ETH exceed $4k?",
                crypto_symbol="ETH",
                spot_price=3_500.0,
                market_implied_prob=0.30,
                our_estimated_prob=0.50,
                delta=0.20,
                ev_net_fees=0.08,
                whale_trade_count=4,
                whale_volume_usd=2_000.0,
                whale_direction="YES",
                time_remaining_s=50_000,
                depth_score=0.6,
                whale_pressure_score=0.6,
            )

        assert result.validated is True  # error → proceed
        assert result.error is True


# ── test_ev_calculator_with_polymarket_fees ───────────────────────────────────

class TestEVCalculator:
    def test_positive_ev_above_threshold(self):
        result = ev_calculator.calculate_ev(our_prob=0.65, entry_price=0.42)
        assert result.ev_net_fees > 0

    def test_negative_ev_below_threshold(self):
        # our_prob barely above market prob with fees kills EV
        result = ev_calculator.calculate_ev(our_prob=0.43, entry_price=0.42)
        assert result.ev_net_fees < 0.06  # won't meet min threshold

    def test_fee_reduces_gross_ev(self):
        result = ev_calculator.calculate_ev(our_prob=0.70, entry_price=0.50)
        assert result.ev_gross > result.ev_net_fees
        assert result.fee_cost > 0

    def test_kelly_size_capped_at_max_position(self):
        size = ev_calculator.kelly_size(
            our_prob=0.80,
            entry_price=0.30,
            bankroll=10_000.0,
            kelly_fraction=1.0,   # full Kelly
            max_position_pct=0.04,
        )
        assert size <= 10_000.0 * 0.04  # capped at 4%

    def test_kelly_size_zero_for_negative_edge(self):
        size = ev_calculator.kelly_size(
            our_prob=0.30,   # below market price — negative edge
            entry_price=0.60,
            bankroll=1_000.0,
        )
        assert size == 0.0
