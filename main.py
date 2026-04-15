"""
main.py — Polymarket Crypto Agents entry point.

Usage:
    python main.py --dry-run                    # mocks, no credentials needed
    python main.py --paper-trade                # real data, no real orders
    python main.py --backtest-only              # recalibrate and exit
    python main.py --paper-trade --bankroll 2000 --config my_config.json

Main loop (paper-trade / dry-run):
  1. Start Binance feed (WebSocket or mock)
  2. Start Polymarket feed (WebSocket + REST fallback or mock)
  3. Start WhaleDetector (polling or mock)
  4. PaperWallet initialised with bankroll and risk controls
  5. For every MarketSnapshot received:
     a. Get Binance price for crypto_symbol
     b. Get active whale signal
     c. Run signal_router.evaluate()
     d. If Signal → wallet risk check → slippage → execute paper trade
  6. Every 30s — PositionManager: mark-to-market, trailing stop, circuit breakers
  7. Every 60s — equity curve snapshot
  8. Every 300s — recalibrate from historical results
  9. Dashboard FastAPI on port 8090 (background)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

import uvicorn

from backtesting import backtest_runner, category_blocker, param_injector
from backtesting.outcome_resolver import resolve_open_trades_from_api
from core import database
from core.config import load_config
from dashboard import api_server
from engine.llm_validator import LLMValidator
from engine.orderbook_analyzer import MockOrderbookAnalyzer, OrderbookAnalyzer
from engine.signal_router import SignalRouter
from execution.order_executor import PaperOrderExecutor, create_executor
from execution.paper_wallet import PaperWallet
from execution.position_manager import PositionManager


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for lib in ("websockets", "aiohttp", "uvicorn.access"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket Crypto Agents")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--paper-trade", action="store_true")
    mode.add_argument("--backtest-only", action="store_true")
    p.add_argument("--config", default="config.json")
    p.add_argument("--bankroll", type=float)
    return p.parse_args()


async def run(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    if args.bankroll:
        cfg.bankroll_usd = args.bankroll

    _configure_logging(cfg.log_level)
    logger = logging.getLogger("main")

    mode = "dry-run" if args.dry_run else ("paper-trade" if args.paper_trade else "backtest-only")
    logger.info(
        "Starting Polymarket Crypto Agents — mode=%s bankroll=$%.0f", mode, cfg.bankroll_usd
    )

    await database.init_db()

    # ── Backtest-only ─────────────────────────────────────────────────────────
    if args.backtest_only:
        logger.info("Backtest mode: resolving open trades and recalibrating…")
        n = await resolve_open_trades_from_api()
        logger.info("Resolved %d trades", n)
        results = await backtest_runner.recalibrate(cfg)
        for sym, wr in results.items():
            logger.info("  %s win_rate=%.2f%%", sym, wr * 100)
        await database.close_db()
        return

    # ── Feed selection ────────────────────────────────────────────────────────
    if args.dry_run:
        from core.models import Side, WhaleSignal
        from feeds.binance_feed import MockBinanceFeed
        from feeds.polymarket_feed import MockPolymarketFeed
        from intelligence.whale_detector import MockWhaleDetector

        binance_feed = MockBinanceFeed()
        market_feed = MockPolymarketFeed()
        whale_detector = MockWhaleDetector(signals={
            "0xabc001": WhaleSignal(
                condition_id="0xabc001",
                direction=Side.YES,
                total_volume_usd=4_500.0,
                trade_count=5,
                avg_price=0.43,
                pressure_score=0.82,
            )
        })
        ob_analyzer = MockOrderbookAnalyzer()
    else:
        import aiohttp
        from feeds.binance_feed import BinanceFeed
        from feeds.polymarket_feed import PolymarketFeed
        from intelligence.whale_detector import WhaleDetector

        _session = aiohttp.ClientSession()
        binance_feed = BinanceFeed()
        market_feed = PolymarketFeed(cfg, _session)
        whale_detector = WhaleDetector(cfg, _session)
        ob_analyzer = OrderbookAnalyzer(
            manipulation_threshold=cfg.orderbook_manipulation_threshold,
            session=_session,
        )

    # ── Paper wallet + engine ─────────────────────────────────────────────────
    wallet = PaperWallet(cfg.bankroll_usd, cfg)
    llm_validator = LLMValidator(cfg) if cfg.llm_validation_enabled else None
    router = SignalRouter(
        cfg=cfg,
        orderbook_analyzer=ob_analyzer,
        llm_validator=llm_validator,
        bankroll=cfg.bankroll_usd,
    )
    executor = PaperOrderExecutor(cfg, wallet)
    position_manager = PositionManager(
        executor=executor,
        signal_router=router,
        wallet=wallet,
        cfg=cfg,
        session=None,
        poll_interval_s=30,
    )

    # ── Dashboard ─────────────────────────────────────────────────────────────
    api_server.configure(
        mode=mode,
        market_feed=market_feed,
        whale_detector=whale_detector,
        position_manager=position_manager,
        wallet=wallet,
    )

    dashboard_config = uvicorn.Config(
        app=api_server.app,
        host="0.0.0.0",
        port=8090,
        log_level="warning",
    )
    dashboard_server = uvicorn.Server(dashboard_config)

    # ── Start subsystems ──────────────────────────────────────────────────────
    await binance_feed.start()
    await market_feed.start()
    await whale_detector.start()
    await position_manager.start()

    logger.info("All feeds active. Dashboard at http://0.0.0.0:8090")
    logger.info("Paper wallet: $%.0f | max_positions=%d | daily_loss_limit=%.0f%%",
                cfg.bankroll_usd, cfg.max_concurrent_positions,
                cfg.daily_loss_limit_pct * 100)

    last_recalib = time.time()
    recalib_interval = 300

    async def market_evaluation_loop() -> None:
        nonlocal last_recalib
        while True:
            try:
                markets = market_feed.get_active_markets()
                for market in markets:
                    price = binance_feed.get_price(market.crypto_symbol)
                    whale = whale_detector.get_signal(market.condition_id)

                    setup_wr, setup_n = await backtest_runner.get_setup_stats(
                        market.condition_id, market.crypto_symbol
                    )
                    signal = await router.evaluate(
                        market, price, whale,
                        setup_win_rate=setup_wr,
                        setup_sample_count=setup_n or 0,
                    )

                    if signal is None:
                        continue

                    # Check wallet risk controls before trying to fill
                    unrealized = position_manager.get_unrealized_pnl()
                    ok, reason = wallet.can_open(
                        signal.crypto_symbol, signal.size_usd, unrealized
                    )
                    if not ok:
                        logging.getLogger("main").info(
                            "WALLET BLOCK %s: %s", signal.condition_id, reason
                        )
                        continue

                    trade = await executor.execute(signal)
                    if trade is None:
                        continue  # fill rejected (thin book or wallet)

                    position_manager.register_trade(trade, signal.crypto_symbol)
                    router.update_bankroll(wallet.nav(unrealized))

                    await database.insert_signal(
                        condition_id=signal.condition_id,
                        crypto_symbol=signal.crypto_symbol,
                        delta=signal.delta,
                        ev_net=signal.ev_net_fees,
                        our_prob=signal.our_prob,
                        market_prob=signal.market_prob,
                        whale_score=signal.whale_score,
                        llm_validated=signal.llm_validated,
                        llm_reason=signal.llm_reason,
                        quality_score=signal.quality_score,
                        timestamp=signal.timestamp,
                    )

                    if whale:
                        await database.insert_whale_event(
                            trade_id=f"whale-{trade.id}",
                            condition_id=signal.condition_id,
                            direction=whale.direction.value,
                            size_usd=whale.total_volume_usd,
                            price=whale.avg_price,
                            timestamp=whale.timestamp,
                        )

                if time.time() - last_recalib > recalib_interval:
                    await backtest_runner.recalibrate(cfg)
                    await param_injector.inject_calibrated_params(cfg)
                    await category_blocker.check_and_block_categories(cfg)
                    last_recalib = time.time()

                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logging.getLogger("main").error(
                    "Eval loop error: %s", exc, exc_info=True
                )
                await asyncio.sleep(5)

    eval_task = asyncio.create_task(market_evaluation_loop(), name="eval_loop")
    try:
        await dashboard_server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        eval_task.cancel()
        await asyncio.gather(eval_task, return_exceptions=True)
        await binance_feed.stop()
        await market_feed.stop()
        await whale_detector.stop()
        await position_manager.stop()
        await database.close_db()
        logger.info("Shutdown complete. Final NAV: $%.2f", wallet.nav())


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
