"""tests/test_execution.py — Unit tests for paper wallet, slippage, executor, position manager."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import AppConfig
from core.models import CloseReason, Side, Signal, Trade, TradeStatus
from execution.paper_wallet import PaperWallet, RiskLimitBreached
from execution.slippage import simulate_entry_fill, simulate_exit_fill
from execution.order_executor import PaperOrderExecutor


def make_cfg(**kwargs) -> AppConfig:
    cfg = AppConfig()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def make_signal(
    condition_id: str = "0xtest001",
    crypto_symbol: str = "BTC",
    side: Side = Side.YES,
    entry_price: float = 0.45,
    size_usd: float = 40.0,
    delta: float = 0.18,
    ev_net_fees: float = 0.10,
    our_prob: float = 0.63,
    market_prob: float = 0.45,
    depth_score: float = 0.75,
) -> Signal:
    return Signal(
        condition_id=condition_id,
        crypto_symbol=crypto_symbol,
        question="Will BTC exceed $70k?",
        side=side,
        entry_price=entry_price,
        size_usd=size_usd,
        delta=delta,
        ev_net_fees=ev_net_fees,
        our_prob=our_prob,
        market_prob=market_prob,
        whale_score=0.0,
        depth_score=depth_score,
    )


def make_open_trade(
    trade_id: str = "trade-001",
    condition_id: str = "0xtest001",
    side: Side = Side.YES,
    size_usd: float = 40.0,
    entry_price: float = 0.459,
) -> Trade:
    return Trade(
        id=trade_id,
        condition_id=condition_id,
        side=side,
        size_usd=size_usd,
        entry_price=entry_price,
        status=TradeStatus.OPEN,
        open_ts=time.time() - 3600,
        peak_price=entry_price,
    )


# ── PaperWallet ───────────────────────────────────────────────────────────────

class TestPaperWallet:
    def test_deduct_reduces_cash(self):
        wallet = PaperWallet(1_000.0, make_cfg())
        wallet.deduct("BTC", 40.0)
        assert wallet.cash == pytest.approx(960.0)

    def test_credit_restores_cash_plus_profit(self):
        wallet = PaperWallet(1_000.0, make_cfg())
        wallet.deduct("BTC", 40.0)
        wallet.credit("BTC", 40.0, pnl=10.0)
        assert wallet.cash == pytest.approx(1_010.0)

    def test_credit_with_loss(self):
        wallet = PaperWallet(1_000.0, make_cfg())
        wallet.deduct("BTC", 40.0)
        wallet.credit("BTC", 40.0, pnl=-15.0)
        assert wallet.cash == pytest.approx(985.0)

    def test_max_concurrent_blocks_new_trade(self):
        cfg = make_cfg(max_concurrent_positions=2)
        wallet = PaperWallet(1_000.0, cfg)
        wallet.deduct("BTC", 10.0)
        wallet.deduct("ETH", 10.0)
        ok, reason = wallet.can_open("SOL", 10.0)
        assert not ok
        assert "max_concurrent" in reason

    def test_symbol_exposure_limit(self):
        cfg = make_cfg(max_symbol_exposure_pct=0.10)
        wallet = PaperWallet(1_000.0, cfg)
        wallet.deduct("BTC", 90.0)   # 9% of $1000
        ok, reason = wallet.can_open("BTC", 30.0)  # would push to 12%
        assert not ok
        assert "max_symbol_exposure" in reason

    def test_insufficient_cash_blocks_trade(self):
        # max_symbol_exposure_pct=2.0 → limit=$200; order=$200 passes that check
        # but cash=$100 < $200 → insufficient_cash fires
        wallet = PaperWallet(100.0, make_cfg(max_symbol_exposure_pct=2.0))
        ok, reason = wallet.can_open("BTC", 200.0)
        assert not ok
        assert "insufficient_cash" in reason

    def test_consecutive_loss_cooldown(self):
        cfg = make_cfg(consecutive_loss_threshold=2, consecutive_loss_cooldown_s=3600)
        wallet = PaperWallet(1_000.0, cfg)
        wallet.deduct("BTC", 20.0)
        wallet.credit("BTC", 20.0, pnl=-5.0)   # loss 1
        wallet.deduct("BTC", 20.0)
        wallet.credit("BTC", 20.0, pnl=-5.0)   # loss 2 → cooldown
        ok, reason = wallet.can_open("ETH", 10.0)
        assert not ok
        assert "cooldown" in reason

    def test_win_resets_consecutive_losses(self):
        cfg = make_cfg(consecutive_loss_threshold=3, consecutive_loss_cooldown_s=3600)
        wallet = PaperWallet(1_000.0, cfg)
        for _ in range(2):
            wallet.deduct("BTC", 10.0)
            wallet.credit("BTC", 10.0, pnl=-1.0)
        # Win before hitting threshold
        wallet.deduct("BTC", 10.0)
        wallet.credit("BTC", 10.0, pnl=5.0)
        ok, _ = wallet.can_open("ETH", 10.0)
        assert ok  # no cooldown

    def test_drawdown_pct(self):
        wallet = PaperWallet(1_000.0, make_cfg())
        wallet.deduct("BTC", 100.0)
        wallet.credit("BTC", 100.0, pnl=-50.0)   # NAV now $950
        dd = wallet.drawdown_pct()
        assert dd == pytest.approx(0.05, abs=0.01)


# ── Slippage ──────────────────────────────────────────────────────────────────

class TestSlippage:
    def test_thin_book_rejects_fill(self):
        cfg = make_cfg(min_fill_depth_score=0.10, partial_fill_depth_threshold=0.25)
        result = simulate_entry_fill(0.45, 40.0, 10_000, depth_score=0.05, cfg=cfg)
        assert result.filled is False
        assert result.fill_fraction == 0.0

    def test_shallow_book_gives_partial_fill(self):
        cfg = make_cfg(min_fill_depth_score=0.10, partial_fill_depth_threshold=0.50,
                       slippage_impact_factor=0.002)
        result = simulate_entry_fill(0.45, 40.0, 10_000, depth_score=0.30, cfg=cfg)
        assert result.filled is True
        assert result.fill_fraction < 1.0
        assert result.fill_fraction > 0.0

    def test_deep_book_gives_full_fill(self):
        cfg = make_cfg(min_fill_depth_score=0.10, partial_fill_depth_threshold=0.25,
                       slippage_impact_factor=0.002)
        result = simulate_entry_fill(0.45, 40.0, 50_000, depth_score=0.90, cfg=cfg)
        assert result.filled is True
        assert result.fill_fraction == 1.0

    def test_large_order_has_more_slippage_than_small(self):
        cfg = make_cfg(min_fill_depth_score=0.10, partial_fill_depth_threshold=0.25,
                       slippage_impact_factor=0.002)
        small = simulate_entry_fill(0.45, 10.0, 10_000, depth_score=0.80, cfg=cfg)
        large = simulate_entry_fill(0.45, 500.0, 10_000, depth_score=0.80, cfg=cfg)
        assert large.slippage >= small.slippage

    def test_slippage_never_exceeds_cap(self):
        cfg = make_cfg(min_fill_depth_score=0.10, partial_fill_depth_threshold=0.25,
                       slippage_impact_factor=0.002)
        result = simulate_entry_fill(0.45, 100_000.0, 1_000, depth_score=0.80, cfg=cfg)
        assert result.slippage <= 0.03  # 3% cap


# ── test_paper_executor_simulates_fill ────────────────────────────────────────

class TestPaperExecutorSimulatesFill:
    @pytest.mark.asyncio
    async def test_fill_deducts_from_wallet(self):
        cfg = make_cfg(max_concurrent_positions=8, max_symbol_exposure_pct=0.5,
                       min_fill_depth_score=0.10, partial_fill_depth_threshold=0.25,
                       slippage_impact_factor=0.002)
        wallet = PaperWallet(1_000.0, cfg)
        executor = PaperOrderExecutor(cfg, wallet)

        with patch("core.database.insert_trade", new_callable=AsyncMock), \
             patch("core.database.insert_journal_entry", new_callable=AsyncMock):
            trade = await executor.execute(make_signal(size_usd=100.0, depth_score=0.80))

        assert trade is not None
        assert wallet.cash < 1_000.0   # deducted

    @pytest.mark.asyncio
    async def test_fill_rejected_for_thin_book(self):
        cfg = make_cfg(min_fill_depth_score=0.50)
        wallet = PaperWallet(1_000.0, cfg)
        executor = PaperOrderExecutor(cfg, wallet)

        with patch("core.database.insert_trade", new_callable=AsyncMock), \
             patch("core.database.insert_journal_entry", new_callable=AsyncMock):
            trade = await executor.execute(make_signal(depth_score=0.20))  # below 0.50

        assert trade is None
        assert wallet.cash == 1_000.0  # nothing deducted

    @pytest.mark.asyncio
    async def test_wallet_risk_blocks_trade(self):
        cfg = make_cfg(max_concurrent_positions=1)
        wallet = PaperWallet(1_000.0, cfg)
        executor = PaperOrderExecutor(cfg, wallet)

        # Fill first trade to hit concurrent limit
        with patch("core.database.insert_trade", new_callable=AsyncMock), \
             patch("core.database.insert_journal_entry", new_callable=AsyncMock):
            t1 = await executor.execute(make_signal(condition_id="0xfirst", size_usd=40.0, depth_score=0.80))
        assert t1 is not None

        # Second trade should be blocked
        with patch("core.database.insert_trade", new_callable=AsyncMock), \
             patch("core.database.insert_journal_entry", new_callable=AsyncMock):
            t2 = await executor.execute(make_signal(condition_id="0xsecond", size_usd=40.0, depth_score=0.80))
        assert t2 is None


# ── test_position_closed_on_resolution ────────────────────────────────────────

class TestPositionClosedOnResolution:
    @pytest.mark.asyncio
    async def test_yes_trade_profitable_on_yes_resolution(self):
        cfg = make_cfg(min_fill_depth_score=0.05, slippage_impact_factor=0.0)
        wallet = PaperWallet(1_000.0, cfg)
        executor = PaperOrderExecutor(cfg, wallet)
        trade = make_open_trade(side=Side.YES, size_usd=40.0, entry_price=0.45)

        with patch("core.database.update_trade_close", new_callable=AsyncMock), \
             patch("core.database.update_journal_close", new_callable=AsyncMock):
            closed = await executor.close_trade_with_symbol(
                trade, exit_price=1.0, symbol="BTC",
                close_reason=CloseReason.MARKET_RESOLVED,
            )

        assert closed.pnl > 0
        assert closed.status == TradeStatus.CLOSED
        assert closed.close_reason == CloseReason.MARKET_RESOLVED

    @pytest.mark.asyncio
    async def test_no_trade_profitable_on_no_resolution(self):
        cfg = make_cfg(min_fill_depth_score=0.05, slippage_impact_factor=0.0)
        wallet = PaperWallet(1_000.0, cfg)
        executor = PaperOrderExecutor(cfg, wallet)
        trade = make_open_trade(side=Side.NO, size_usd=40.0, entry_price=0.45)

        with patch("core.database.update_trade_close", new_callable=AsyncMock), \
             patch("core.database.update_journal_close", new_callable=AsyncMock):
            closed = await executor.close_trade_with_symbol(
                trade, exit_price=0.0, symbol="BTC",
                close_reason=CloseReason.MARKET_RESOLVED,
            )

        assert closed.pnl > 0


# ── test_stop_loss_triggers ───────────────────────────────────────────────────

class TestStopLossTriggers:
    @pytest.mark.asyncio
    async def test_stop_loss_closes_position(self):
        from engine.orderbook_analyzer import MockOrderbookAnalyzer
        from engine.signal_router import SignalRouter
        from execution.position_manager import PositionManager, _HARD_STOP_LOSS

        cfg = make_cfg(min_fill_depth_score=0.05, slippage_impact_factor=0.0,
                       trailing_stop_activation=0.50, trailing_stop_distance=0.30)
        wallet = PaperWallet(1_000.0, cfg)
        router = SignalRouter(cfg, MockOrderbookAnalyzer(), bankroll=1_000.0)
        executor = PaperOrderExecutor(cfg, wallet)

        pm = PositionManager(
            executor=executor,
            signal_router=router,
            wallet=wallet,
            cfg=cfg,
            session=None,
            poll_interval_s=999,
        )

        trade = make_open_trade(entry_price=0.50)
        stop_price = 0.50 * _HARD_STOP_LOSS * 0.9   # 45% below threshold

        with patch.object(pm, "_fetch_market", new_callable=AsyncMock, return_value={
            "current_price": stop_price,
            "time_to_expiry_s": 3600,
            "resolved": False,
            "resolution_price": None,
        }), patch("core.database.update_trade_close", new_callable=AsyncMock), \
           patch("core.database.update_journal_close", new_callable=AsyncMock):
            pm._open_trades[trade.id] = trade
            pm._trade_symbol[trade.id] = "BTC"
            await pm._evaluate_all()

        assert trade.id not in pm._open_trades   # trade was closed

    @pytest.mark.asyncio
    async def test_trailing_stop_triggers_after_peak(self):
        from engine.orderbook_analyzer import MockOrderbookAnalyzer
        from engine.signal_router import SignalRouter
        from execution.position_manager import PositionManager

        cfg = make_cfg(
            trailing_stop_activation=0.10,   # activate at 10% gain
            trailing_stop_distance=0.05,     # close if 5% below peak
            min_fill_depth_score=0.05, slippage_impact_factor=0.0,
        )
        wallet = PaperWallet(1_000.0, cfg)
        router = SignalRouter(cfg, MockOrderbookAnalyzer(), bankroll=1_000.0)
        executor = PaperOrderExecutor(cfg, wallet)
        pm = PositionManager(executor=executor, signal_router=router,
                             wallet=wallet, cfg=cfg, session=None, poll_interval_s=999)

        trade = make_open_trade(entry_price=0.40)
        trade.peak_price = 0.44   # peaked at +10% (activation met)
        # Current price pulls back to 0.417 = 5.2% below peak → trigger
        current_price = 0.44 * (1 - 0.052)

        with patch.object(pm, "_fetch_market", new_callable=AsyncMock, return_value={
            "current_price": current_price,
            "time_to_expiry_s": 3600,
            "resolved": False,
            "resolution_price": None,
        }), patch("core.database.update_trade_close", new_callable=AsyncMock), \
           patch("core.database.update_journal_close", new_callable=AsyncMock):
            pm._open_trades[trade.id] = trade
            pm._trade_symbol[trade.id] = "ETH"
            await pm._evaluate_all()

        assert trade.id not in pm._open_trades   # trailing stop triggered

    @pytest.mark.asyncio
    async def test_trailing_stop_not_triggered_below_activation(self):
        from engine.orderbook_analyzer import MockOrderbookAnalyzer
        from engine.signal_router import SignalRouter
        from execution.position_manager import PositionManager

        cfg = make_cfg(
            trailing_stop_activation=0.20,   # 20% gain required to activate
            trailing_stop_distance=0.05,
            min_fill_depth_score=0.05, slippage_impact_factor=0.0,
        )
        wallet = PaperWallet(1_000.0, cfg)
        router = SignalRouter(cfg, MockOrderbookAnalyzer(), bankroll=1_000.0)
        executor = PaperOrderExecutor(cfg, wallet)
        pm = PositionManager(executor=executor, signal_router=router,
                             wallet=wallet, cfg=cfg, session=None, poll_interval_s=999)

        trade = make_open_trade(entry_price=0.40)
        trade.peak_price = 0.44   # only +10% — not enough to activate trailing stop

        with patch.object(pm, "_fetch_market", new_callable=AsyncMock, return_value={
            "current_price": 0.38,   # would be a pullback but not activated
            "time_to_expiry_s": 3600,
            "resolved": False,
            "resolution_price": None,
        }), patch("core.database.update_trade_close", new_callable=AsyncMock), \
           patch("core.database.update_journal_close", new_callable=AsyncMock):
            pm._open_trades[trade.id] = trade
            pm._trade_symbol[trade.id] = "ETH"
            await pm._evaluate_all()

        # Hard stop not triggered (0.38 > 0.40 * 0.50 = 0.20), trailing not activated
        assert trade.id in pm._open_trades
