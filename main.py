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
from typing import Optional

import uvicorn

from backtesting import backtest_runner, category_blocker, param_injector
from backtesting.outcome_resolver import resolve_open_trades_from_api
from core import database
from core.config import load_config
from dashboard import api_server
from engine.llm_validator import LLMValidator
from engine.market_ranker import MarketRanker, RankResult
from engine.orderbook_analyzer import MockOrderbookAnalyzer, OrderbookAnalyzer
from engine.signal_router import SignalRouter
from engine.volatility import VolatilityProvider
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
    market_ranker = MarketRanker(cfg) if cfg.ranker_enabled else None
    vol_provider = VolatilityProvider(cfg)
    await vol_provider.start()
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
        market_ranker=market_ranker,
        vol_provider=vol_provider,
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
    # cid → (last_ts, last_verdict_stage). A new row is written when either
    # `snapshot_min_interval_s` has elapsed OR the verdict stage changed.
    snapshot_last_logged: dict[str, tuple[float, str]] = {}

    async def _log_snapshot(
        market, price, whale, decision, rank: Optional[RankResult]
    ) -> None:
        if not cfg.snapshot_logging_enabled:
            return
        now = time.time()
        current_key = (
            "accepted" if decision.accepted
            else f"rejected:{decision.stage}"
        )
        prev = snapshot_last_logged.get(market.condition_id)
        if prev is not None:
            prev_ts, prev_key = prev
            verdict_flipped = current_key != prev_key
            if (not verdict_flipped
                    and (now - prev_ts) < cfg.snapshot_min_interval_s):
                return
        snapshot_last_logged[market.condition_id] = (now, current_key)
        whale_score = whale.pressure_score if whale else 0.0
        whale_count = whale.trade_count if whale else 0
        try:
            await database.insert_market_snapshot(
                timestamp=now,
                condition_id=market.condition_id,
                token_id=market.token_id,
                question=market.question,
                crypto_symbol=market.crypto_symbol,
                binance_price=(price.price if price else None),
                market_implied_prob=market.implied_prob,
                best_bid=market.best_bid,
                best_ask=market.best_ask,
                spread=market.spread,
                volume_24h=market.volume_24h,
                open_interest=market.open_interest,
                time_to_expiry_s=market.time_to_expiry_s,
                whale_score=whale_score,
                whale_count=whale_count,
                ranker_score=(rank.score if rank else None),
                ranker_reason=(rank.reason if rank else ""),
                verdict=("accepted" if decision.accepted else "rejected"),
                reject_stage=decision.stage,
                reject_reason=decision.reason,
                signal_delta=decision.delta,
                ev_net=decision.ev_net,
            )
        except Exception as exc:
            logging.getLogger("main").warning("snapshot log failed: %s", exc)

    async def market_evaluation_loop() -> None:
        nonlocal last_recalib
        from engine import probability
        from engine.signal_router import RouterDecision
        while True:
            try:
                markets = market_feed.get_active_markets()
                ranker_api_calls_this_scan = 0

                for market in markets:
                    price = binance_feed.get_price(market.crypto_symbol)
                    whale = whale_detector.get_signal(market.condition_id)

                    # Pull realized volatility (cached per symbol ~10 min) so the
                    # probability engine can use Black-Scholes instead of the
                    # time-independent logistic. Falls back to None on failure,
                    # in which case probability.estimate uses the legacy path.
                    sigma = await vol_provider.get(market.crypto_symbol)

                    # Pre-compute probability estimate once so both the ranker
                    # and the router see the same numbers.
                    prob_est = probability.estimate(
                        market, price, whale, volatility=sigma,
                    )

                    # ── Market ranker pre-filter (optional) ─────────────────
                    rank_result: Optional[RankResult] = None
                    if market_ranker is not None:
                        over_budget = ranker_api_calls_this_scan >= cfg.ranker_max_per_scan
                        if not over_budget:
                            rank_result = await market_ranker.rank(
                                market, price, whale, prob_est=prob_est,
                            )
                            # Only real HTTP calls consume the scan budget.
                            # Cache hits and skipped calls are free.
                            if not rank_result.from_cache and not rank_result.skipped:
                                ranker_api_calls_this_scan += 1
                        if (rank_result is not None
                                and rank_result.score is not None
                                and rank_result.score < cfg.ranker_min_score):
                            await _log_snapshot(
                                market, price, whale,
                                RouterDecision(
                                    signal=None,
                                    stage="ranker",
                                    reason=f"score={rank_result.score:.2f} < {cfg.ranker_min_score:.2f}: {rank_result.reason}",
                                ),
                                rank_result,
                            )
                            continue

                    setup_wr, setup_n = await backtest_runner.get_setup_stats(
                        market.condition_id, market.crypto_symbol
                    )
                    decision = await router.evaluate_full(
                        market, price, whale,
                        setup_win_rate=setup_wr,
                        setup_sample_count=setup_n or 0,
                        prob_est=prob_est,
                    )

                    await _log_snapshot(market, price, whale, decision, rank_result)
                    signal = decision.signal

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
        await vol_provider.stop()
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
