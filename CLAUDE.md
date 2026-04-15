# Polymarket Crypto Agents — CLAUDE.md

> **Invariant: if this file contradicts the code, the code wins.**
> This document describes architecture and intent; the source files are canonical.

---

## Project Overview

Autonomous paper-trading bot for Polymarket crypto prediction markets.
Uses Binance as the **sole** external price feed. Detects whale trades on
Polymarket and routes signals through a multi-stage filter pipeline.

**Default mode**: paper trading (simulated orders, real market data).

---

## File Architecture

```
main.py                — CLI entry point, async event loop, mode selection
config.json            — All numeric thresholds (editable without code changes)
core/
  models.py            — Canonical data classes (PriceSnapshot, MarketSnapshot, Signal, …)
  config.py            — AppConfig loader (config.json + env vars)
  database.py          — SQLite schema + async CRUD helpers
  interfaces.py        — ABCs for feeds, analyzers, executor (testability boundary)
feeds/
  binance_feed.py      — BinanceFeed (WS) + MockBinanceFeed (dry-run/tests)
  polymarket_feed.py   — PolymarketFeed (REST discovery + WS streaming) + Mock
engine/
  probability.py       — Spot-price → implied probability + whale adjustment
  timing.py            — Timing confidence / size multipliers
  ev_calculator.py     — EV formula, Polymarket 2% fee model, Kelly sizing
  signal_router.py     — 9-stage filter pipeline; produces Signal or None
  orderbook_analyzer.py— Top-3 concentration manipulation check + effective spread
  llm_validator.py     — OpenRouter (claude-3-haiku) optional validation gate
intelligence/
  whale_detector.py    — Polls /activity, filters ≥$500, aggregates WhaleSignal
execution/
  order_executor.py    — PaperOrderExecutor (default) + ProductionOrderExecutor stub
  position_manager.py  — 30s polling loop: resolved/stop-loss/lock-profit closures
backtesting/
  backtest_runner.py   — Win-rate recalibration from closed trades
  param_injector.py    — Tightens category thresholds on poor win-rate
  category_blocker.py  — Blocks categories with WR < 40% for 24h
  outcome_resolver.py  — Resolves open trades from Polymarket API (--backtest-only)
dashboard/
  api_server.py        — FastAPI on port 8090; 6 REST endpoints
  static/index.html    — Vanilla JS dashboard, auto-refreshes every 10s
tests/
  test_engine.py       — Probability, signal router, LLM validator, EV calculator
  test_execution.py    — Paper executor fills, position close, stop-loss
  test_integration.py  — Full pipeline (dry-run), whale filter
  test_schema.py       — SQLite schema, CRUD
```

---

## Startup Modes

| Command | What it does |
|---|---|
| `python main.py --dry-run` | Mock feeds + mock whale detector. No credentials needed. Runs full pipeline with synthetic data. Dashboard on :8090. |
| `python main.py --paper-trade` | Real Binance WS + real Polymarket REST/WS. No real orders. Needs no wallet key. Credentials optional (public endpoints used without auth). |
| `python main.py --backtest-only` | Resolves open paper trades against Polymarket API, recalibrates win-rates per symbol, prints summary, exits. |
| `python main.py --paper-trade --config my.json --bankroll 2000` | Overrides config path and starting bankroll. |

---

## Signal Pipeline (9 stages)

Every MarketSnapshot passes through `engine/signal_router.py`:

1. **Liquidity** — volume_24h, open_interest, spread vs config thresholds
2. **Timing** — time_to_expiry_s within [min, max]; near-expiry handling
3. **Orderbook** — top-3 concentration > 60% → manipulated → reject
4. **Probability** — Binance spot vs. strike → delta calculation + whale ±0.05
5. **EV** — ev_net_fees = ev_gross − 2% fee must exceed min_ev_threshold
6. **Price bounds** — entry_price ∈ [0.05, 0.95]
7. **Setup quality gate** — historical win-rate ≥ 52% (if ≥ 10 samples)
8. **LLM validator** — OpenRouter claude-3-haiku, only if whale_score ≥ 0.5
9. **Deduplication** — no double positions on the same condition_id

---

## Key Design Decisions

- **Binance only** for price feeds. Hyperliquid and all other external sources
  are excluded by design. MockBinanceFeed provides static prices for tests.
- **No py-clob-client** — all Polymarket REST/WS calls are implemented directly.
- **LLM is advisory** — on timeout or error, the signal proceeds (non-blocking).
- **Paper trade default** — `PAPER_TRADE=true` in env; production requires
  explicit `ENV=production PAPER_TRADE=false` + wallet private key.
- **Category overrides** — `config.json["category_overrides"]["BTC"]` applies
  tighter thresholds for specific symbols. No code change required.
- **SQLite WAL mode** — concurrent reads from the dashboard don't block writes
  from the trading loop.

---

## Environment Variables

See `.env.example`. Key variables:

| Variable | Default | Notes |
|---|---|---|
| `PAPER_TRADE` | `true` | Set `false` only in production |
| `ENV` | `development` | Must be `production` for live orders |
| `BANKROLL_USD` | `1000` | Overridable with `--bankroll` |
| `LOG_LEVEL` | `INFO` | Set `DEBUG` for verbose output |
| `OPENROUTER_API_KEY` | — | Optional; LLM gate skipped if absent |
| `BINANCE_API_KEY` | — | Optional for public streams |
| `POLYMARKET_WALLET_PRIVATE_KEY` | — | Only needed for live trading |

---

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

All tests use mocks — no real network calls are made in the test suite.
