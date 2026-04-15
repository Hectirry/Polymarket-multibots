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

## Configuration

Edit `config.json` to tune thresholds without touching code.
See `CLAUDE.md` for full architecture documentation.

## Stack

Python 3.11+ · FastAPI · aiohttp · websockets · SQLite · Binance WS feed
