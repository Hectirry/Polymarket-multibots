"""
core/models.py — Shared data models for the entire system.

All domain objects are defined here as dataclasses or Pydantic models.
These are the canonical shapes passed between modules — if a field is absent
here it does not officially exist in the system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class CloseReason(str, Enum):
    MARKET_RESOLVED   = "market_resolved"
    STOP_LOSS         = "stop_loss"
    TRAILING_STOP     = "trailing_stop"
    LOCK_PROFIT       = "lock_profit_near_expiry"
    DAILY_LOSS_LIMIT  = "daily_loss_limit"
    MANUAL            = "manual"


@dataclass
class PriceSnapshot:
    """Spot price from Binance WebSocket feed."""

    symbol: str          # e.g. "BTC", "ETH"
    price: float
    timestamp_ms: int
    source: str = "binance"


@dataclass
class MarketSnapshot:
    """Live state of a Polymarket prediction market."""

    condition_id: str
    token_id: str                   # YES token id
    question: str
    crypto_symbol: str              # e.g. "BTC"

    implied_prob: float             # best_bid of YES token ≈ P(YES)
    best_bid: float
    best_ask: float
    spread: float

    volume_24h: float
    open_interest: float

    time_to_expiry_s: float
    fraction_of_life_elapsed: float  # elapsed / total_duration

    resolved: bool = False
    resolution_price: Optional[float] = None

    timestamp: float = field(default_factory=time.time)


@dataclass
class WhaleSignal:
    """Aggregated whale activity for a single market."""

    condition_id: str
    direction: Side
    total_volume_usd: float
    trade_count: int
    avg_price: float
    pressure_score: float   # 0.0 – 1.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class OrderbookAnalysis:
    """Output of orderbook_analyzer for a single market."""

    token_id: str
    is_manipulated: bool
    effective_spread: float
    depth_score: float          # 0.0 = shallow, 1.0 = deep
    top3_concentration: float   # fraction held by top 3 orders


@dataclass
class FillResult:
    """Result of a simulated paper fill, including slippage details."""

    filled: bool                  # False → rejected (book too thin)
    fill_price: float             # actual execution price after slippage
    requested_price: float        # price from signal
    slippage: float               # fill_price - requested_price (positive = worse)
    fill_fraction: float          # 1.0 = full fill; <1 = partial
    actual_size_usd: float        # actual $ risked (fill_fraction * requested)
    fee_usd: float                # fee paid


@dataclass
class Signal:
    """A trade signal that has passed all router filters."""

    condition_id: str
    crypto_symbol: str
    question: str

    side: Side
    entry_price: float          # YES token price to buy/sell at
    size_usd: float             # dollar amount to risk

    delta: float                # binance_prob - market_prob
    ev_net_fees: float
    our_prob: float
    market_prob: float
    whale_score: float

    llm_validated: bool = False
    llm_reason: str = ""
    quality_score: float = 1.0

    # Orderbook context captured at signal time (for slippage model)
    depth_score: float = 0.5
    effective_spread: float = 0.03

    timestamp: float = field(default_factory=time.time)


@dataclass
class Trade:
    """A paper or live trade record."""

    id: str
    condition_id: str
    side: Side
    size_usd: float
    entry_price: float

    status: TradeStatus = TradeStatus.OPEN
    exit_price: Optional[float] = None
    pnl: float = 0.0
    close_reason: Optional[CloseReason] = None

    open_ts: float = field(default_factory=time.time)
    close_ts: Optional[float] = None

    # Real-time tracking (not persisted — recomputed each evaluation cycle)
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    peak_price: float = 0.0       # highest favourable price seen (for trailing stop)
    max_favorable_excursion: float = 0.0   # best unrealized P&L ever seen
    max_adverse_excursion: float = 0.0     # worst unrealized P&L seen (negative)

    # Slippage stats
    slippage_usd: float = 0.0
    fill_fraction: float = 1.0


@dataclass
class WhaleEvent:
    """Single whale trade event from the activity feed."""

    trade_id: str
    condition_id: str
    side: Side
    size_usd: float
    price: float
    timestamp: float


@dataclass
class EquityPoint:
    """Single snapshot of portfolio NAV for the equity curve."""

    timestamp: float
    nav: float                    # bankroll + unrealized_pnl
    cash: float                   # available cash
    unrealized_pnl: float
    realized_pnl_cumulative: float
    open_positions: int


@dataclass
class TradeJournalEntry:
    """Full signal context captured at trade entry — never mutated after write."""

    trade_id: str
    condition_id: str
    crypto_symbol: str
    question: str
    side: str

    # Signal quality at entry
    delta: float
    ev_net: float
    our_prob: float
    market_prob: float
    whale_score: float
    depth_score: float
    quality_score: float
    llm_validated: bool
    llm_reason: str

    # Fill details
    requested_price: float
    fill_price: float
    slippage: float
    fill_fraction: float
    size_usd: float
    fee_usd: float

    open_ts: float

    # Filled in at close
    close_reason: Optional[str] = None
    hold_time_s: Optional[float] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    max_favorable_excursion: Optional[float] = None
    max_adverse_excursion: Optional[float] = None


@dataclass
class PaperAnalytics:
    """Snapshot of all paper-trading performance metrics."""

    # Overall
    total_trades: int
    open_trades: int
    win_rate: float
    profit_factor: float          # gross_profit / gross_loss
    expectancy: float             # avg $ per trade
    total_pnl: float
    total_fees_paid: float
    total_slippage: float

    # Risk metrics
    sharpe_ratio: float
    max_drawdown_pct: float
    max_drawdown_usd: float
    current_drawdown_pct: float

    # Hold time
    avg_hold_time_s: float
    median_hold_time_s: float

    # Streak
    current_streak: int           # positive = wins, negative = losses
    max_win_streak: int
    max_loss_streak: int

    # Per-symbol breakdown
    by_symbol: dict[str, dict]

    # Wallet state
    nav: float
    cash: float
    initial_bankroll: float
    total_return_pct: float

    # Close reasons
    closes_by_reason: dict[str, int]
