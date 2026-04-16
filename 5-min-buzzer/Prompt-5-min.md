# POLYMARKET TRADING BOT - MASTER PROMPT FOR CLAUDE CODE

## CONTEXT
I'm Hect, an Industrial Engineer from Colombia (Medellín/Bucaramanga). 
I'm building automated trading bots for Polymarket using Claude Code.
My VPS just restarted and I lost all progress. 
I need to rebuild my bot infrastructure with better persistence and scalability.

Previous bot: 5min_buzzer_beater.py (Bitcoin UP/DOWN, "sit on ask" strategy)
Current status: Need to pivot to less saturated markets + add resilience

## REQUIREMENTS

### Core Bot Features:
1. **Multi-Strategy Support** - Can run stink bid OR sit on ask OR custom
2. **Market Flexibility** - Cricket, Tennis, Football, Bitcoin (any Polymarket market)
3. **Persistent State** - Survives VPS restarts (logs trades, saves config)
4. **Multi-Account** - Support 3-5 Polymarket accounts in parallel
5. **Risk Controls** - Max bet size, max exposure, position limits
6. **Real-Time Monitoring** - Dashboard showing live positions, P&L, fill rates
7. **Error Handling** - Graceful failures, automatic retries, notifications
8. **Logging** - Every action logged to JSON (orders, fills, cancels, errors)

### Tech Stack:
- Language: Python 3.10+
- Polymarket API (free, documented)
- ESPN API for sports data (free)
- Persistent storage: SQLite + JSON logs
- Multi-threading for parallel accounts
- Configuration: YAML file (reload without restart)

### Priority Order:
1. **Foundation** - Base bot class with multi-account support
2. **Strategy Layer** - Stink bid template (default)
3. **Data Layer** - Market monitoring, order management, position tracking
4. **Resilience** - Logging, recovery, notifications
5. **Visualization** - Simple CLI dashboard (optional but useful)

## SPECIFIC IMPLEMENTATION DETAILS

### Bot Structure:
polymarket-trading-bot/
├── config.yaml                 # Configuration (accounts, markets, strategies)
├── bot.py                      # Main bot logic
├── strategies/
│   ├── stink_bid.py           # Stink bid strategy (30% below market)
│   ├── sit_on_ask.py          # Sit on ask strategy (aggressive)
│   └── scalper.py             # Quick exits strategy (if useful)
├── data/
│   ├── market_monitor.py      # Real-time market data
│   ├── order_manager.py       # Order placement/cancellation
│   └── position_tracker.py    # P&L, fill rates, metrics
├── persistence/
│   ├── storage.py             # SQLite + JSON logging
│   └── recovery.py            # Restart recovery logic
├── dashboard.py               # CLI monitoring (optional)
├── logs/                       # Trade logs (auto-created)
└── .env                        # API keys (git ignored)

### Key Functions Needed:

**bot.py (Main Class)**
```python
class PolymarketBot:
    def __init__(self, config_path: str, account_id: str):
        # Load config, API keys, previous state
        # Initialize strategies, market monitor, order manager
        
    def run(self):
        # Main loop: monitor → identify opportunities → execute → log
        
    def place_stink_bid(self, market_id: str, side: str, discount_pct: float):
        # Place bid at X% below current market price
        
    def place_sit_on_ask(self, market_id: str, side: str, threshold: float):
        # Place bid at threshold if best ask >= threshold
        
    def cancel_orders(self, market_id: str):
        # Cancel all pending orders in market
        
    def get_position(self, market_id: str):
        # Return current position (size, entry price, unrealized P&L)
        
    def log_action(self, action_type: str, details: dict):
        # Log to JSON + print to console
```

### Config Example (config.yaml):
```yaml
accounts:
  account_1:
    private_key: ${PRIVATE_KEY_1}
    public_key: ${PUBLIC_KEY_1}
    max_exposure_usd: 500
    
strategies:
  stink_bid_cricket:
    type: stink_bid
    discount_pct: 30
    markets: ["IPL", "PSL"]
    bet_size: 5
    
  sit_on_ask_crypto:
    type: sit_on_ask
    threshold: 0.70
    markets: ["BTC_UP_DOWN"]  # Only if needed
    bet_size: 5

monitoring:
  check_interval_sec: 3
  log_to_file: true
  dashboard_enabled: true
```

### Must-Have Behaviors:
1. **On startup:** Check persisted state, resume any incomplete trades
2. **Every 3 sec:** Monitor market prices, check if new opportunities
3. **On order fill:** Log immediately, update position, check exit conditions
4. **On market close:** Roll over to next market OR wait for next cycle
5. **On VPS restart:** Auto-recover all state from logs
6. **On error:** Log, retry 3x, then notify (Slack/email if possible)

## DELIVERABLES

When done, I want:
1. **bot.py** - Fully functional, multi-account, strategy-agnostic
2. **config.yaml** - My accounts + strategies pre-configured
3. **logs/** - Directory with JSON trade history
4. **README.md** - How to run, debug, extend
5. **OPTIONAL: dashboard.py** - CLI showing live positions + P&L

## CONSTRAINTS
- Must handle 5-min markets (fast execution, network latency considerations)
- Must NOT duplicate orders (critical risk control)
- Must support account switching without redeployment
- Must survive VPS restarts (this is non-negotiable)
- Memory footprint: < 500MB (VPS is constrained)

## TONE & STYLE
- Keep code clean, well-commented, production-ready
- Assume I'll modify strategies later (make it extensible)
- Assume I'm running this 24/7 on a VPS (reliability first)
- Assume I'll add more features (good architecture matters)
- Give me SHORT, CONCISE variable names (but clear intent)