# Polymarket Crypto Agents

Autonomous paper-trading bot for Polymarket crypto prediction markets.
Detects whale activity, prices signals via Binance, and routes trades through
a 9-stage filter pipeline. Ships with a live dashboard and full test suite.

## Quick Start (5 commands)

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Copy env file and optionally fill in keys
cp .env.example .env

# 3. Run in dry-run mode (no credentials needed)
python main.py --dry-run

# 4. Run tests
pytest tests/ -v

# 5. Open dashboard
open http://localhost:8090
```

## Modes

| Mode | Command | Needs credentials? |
|---|---|---|
| Demo | `python main.py --dry-run` | No |
| Paper trade | `python main.py --paper-trade` | Optional |
| Recalibrate | `python main.py --backtest-only` | No |

## Running the bot as a supervised service

Use `bot.sh` to run the bot under a supervisor that automatically restarts
it if it crashes. Both supervisor and bot PIDs are tracked via pidfiles so
`stop` cleanly kills both.

```bash
./bot.sh start      # launch supervisor + bot in background (paper-trade mode)
./bot.sh status     # show supervisor + bot process state
./bot.sh logs       # tail the bot log (Ctrl-C to exit)
./bot.sh stop       # stop bot and supervisor
./bot.sh restart    # stop + start
```

**Logs**

- `bot_output.log` — stdout/stderr from the python process
- `supervisor.log` — supervisor lifecycle events (launches, exits, restarts)

**Overrides** (via environment variables):

```bash
BOT_MODE="--dry-run"      ./bot.sh start   # default: --paper-trade
RESTART_DELAY_S=10        ./bot.sh start   # default: 5s backoff between restarts
```

## Market snapshot logger + backtest replay

Every market that reaches the signal router is written to the
`market_snapshots` table along with the router's verdict (accepted or the
reject stage + reason). Dedupe interval: `snapshot_min_interval_s` in
`config.json` (default 60s per `condition_id`).

Replay the captured snapshots:

```bash
python -m backtesting.snapshot_replay --hours 24       # last 24h summary
python -m backtesting.snapshot_replay --hours 6 --top-reasons 15
```

Reports accepted vs rejected, breakdown by reject stage, per-symbol
acceptance rate, and ranker score stats (if enabled).

## LLM market ranker (optional pre-filter)

When `ranker_enabled` is true in `config.json`, each market is scored 0–1
by an LLM on attractiveness (narrative clarity, catalyst proximity, whale
coherence, depth). Markets below `ranker_min_score` are skipped before
the pipeline runs — saving work on noisy markets.

Keys (set in `config.json`):

| Key | Default | Notes |
|---|---|---|
| `ranker_enabled` | `true` | Turn the pre-filter on/off |
| `ranker_model` | `minimax/minimax-01` | Any OpenRouter model ID |
| `ranker_min_score` | `0.55` | Markets scoring below this are skipped |
| `ranker_cache_ttl_s` | `900` | Cache per `condition_id` |
| `ranker_max_per_scan` | `30` | Hard cap on LLM calls per market scan |

**Requires `OPENROUTER_API_KEY`** in `.env`. Without the key the ranker
silently no-ops (neutral score) and the full pipeline runs as before.

## Configuration

Edit `config.json` to tune thresholds without touching code.
See `CLAUDE.md` for full architecture documentation.

## Stack

Python 3.11+ · FastAPI · aiohttp · websockets · SQLite · Binance WS feed
