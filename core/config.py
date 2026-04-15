"""
core/config.py — Runtime configuration loader.

Merges config.json with environment variables.  Category-level overrides
from config.json["category_overrides"] are applied per-symbol so callers
can request symbol-aware thresholds via get_for_symbol().
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    # EV / signal thresholds
    min_ev_threshold: float = 0.06
    min_delta: float = 0.12
    min_contract_price: float = 0.05
    max_contract_price: float = 0.95
    max_market_overround_bps: int = 300

    # Timing
    min_time_remaining_s: float = 300
    max_time_remaining_s: float = 2_592_000   # 30 days

    # Sizing
    max_position_pct: float = 0.04
    kelly_fraction: float = 0.25
    bankroll_usd: float = 1_000.0

    # Market quality
    min_volume_24h: float = 5_000.0
    min_open_interest: float = 1_000.0
    max_spread: float = 0.05

    # Whale detection
    whale_min_trade_usd: float = 500.0
    whale_pressure_window_s: float = 300.0
    whale_min_count_for_signal: int = 3

    # LLM validator
    llm_validation_enabled: bool = True
    llm_min_whale_score: float = 0.5
    llm_cache_ttl_s: float = 600.0

    # Orderbook
    orderbook_manipulation_threshold: float = 0.60

    # Poll intervals
    market_rescan_interval_s: float = 60.0
    whale_poll_interval_s: float = 15.0

    # Setup quality gate
    setup_quality_gate_enabled: bool = True
    setup_quality_min_sample: int = 10
    setup_quality_min_wr: float = 0.52

    # Category-level overrides (symbol → {param: value})
    category_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Paper trading simulation controls
    max_concurrent_positions: int = 8
    max_symbol_exposure_pct: float = 0.12
    daily_loss_limit_pct: float = 0.05
    trailing_stop_activation: float = 0.15
    trailing_stop_distance: float = 0.10
    consecutive_loss_threshold: int = 3
    consecutive_loss_cooldown_s: float = 1800.0
    slippage_impact_factor: float = 0.002
    min_fill_depth_score: float = 0.10
    partial_fill_depth_threshold: float = 0.25
    equity_snapshot_interval_s: float = 60.0

    # Runtime toggles (from env)
    paper_trade: bool = True
    env: str = "development"
    log_level: str = "INFO"

    # API keys (from env)
    polymarket_api_key: str = ""
    polymarket_secret: str = ""
    polymarket_passphrase: str = ""
    polymarket_wallet_private_key: str = ""
    binance_api_key: str = ""
    openrouter_api_key: str = ""

    def get_for_symbol(self, symbol: str, param: str) -> Any:
        """Return category-overridden value for symbol, or the global default."""
        overrides = self.category_overrides.get(symbol.upper(), {})
        if param in overrides:
            return overrides[param]
        return getattr(self, param)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load and merge config.json + environment variables into AppConfig."""
    path = Path(config_path or "config.json")
    raw: dict[str, Any] = {}

    if path.exists():
        with path.open() as f:
            raw = json.load(f)
        logger.info("Loaded config from %s", path)
    else:
        logger.warning("config.json not found, using defaults")

    cfg = AppConfig(
        min_ev_threshold=raw.get("min_ev_threshold", AppConfig.min_ev_threshold),
        min_delta=raw.get("min_delta", AppConfig.min_delta),
        min_contract_price=raw.get("min_contract_price", AppConfig.min_contract_price),
        max_contract_price=raw.get("max_contract_price", AppConfig.max_contract_price),
        max_market_overround_bps=raw.get("max_market_overround_bps", AppConfig.max_market_overround_bps),
        min_time_remaining_s=raw.get("min_time_remaining_s", AppConfig.min_time_remaining_s),
        max_time_remaining_s=raw.get("max_time_remaining_s", AppConfig.max_time_remaining_s),
        max_position_pct=raw.get("max_position_pct", AppConfig.max_position_pct),
        kelly_fraction=raw.get("kelly_fraction", AppConfig.kelly_fraction),
        min_volume_24h=raw.get("min_volume_24h", AppConfig.min_volume_24h),
        min_open_interest=raw.get("min_open_interest", AppConfig.min_open_interest),
        max_spread=raw.get("max_spread", AppConfig.max_spread),
        whale_min_trade_usd=raw.get("whale_min_trade_usd", AppConfig.whale_min_trade_usd),
        whale_pressure_window_s=raw.get("whale_pressure_window_s", AppConfig.whale_pressure_window_s),
        whale_min_count_for_signal=raw.get("whale_min_count_for_signal", AppConfig.whale_min_count_for_signal),
        llm_validation_enabled=raw.get("llm_validation_enabled", AppConfig.llm_validation_enabled),
        llm_min_whale_score=raw.get("llm_min_whale_score", AppConfig.llm_min_whale_score),
        llm_cache_ttl_s=raw.get("llm_cache_ttl_s", AppConfig.llm_cache_ttl_s),
        orderbook_manipulation_threshold=raw.get(
            "orderbook_manipulation_threshold", AppConfig.orderbook_manipulation_threshold
        ),
        market_rescan_interval_s=raw.get("market_rescan_interval_s", AppConfig.market_rescan_interval_s),
        whale_poll_interval_s=raw.get("whale_poll_interval_s", AppConfig.whale_poll_interval_s),
        setup_quality_gate_enabled=raw.get("setup_quality_gate_enabled", AppConfig.setup_quality_gate_enabled),
        setup_quality_min_sample=raw.get("setup_quality_min_sample", AppConfig.setup_quality_min_sample),
        setup_quality_min_wr=raw.get("setup_quality_min_wr", AppConfig.setup_quality_min_wr),
        category_overrides=raw.get("category_overrides", {}),
        # Paper trading params from nested key
        **{k: raw.get("paper_trading", {}).get(k, getattr(AppConfig, k, None))
           for k in [
               "max_concurrent_positions", "max_symbol_exposure_pct",
               "daily_loss_limit_pct", "trailing_stop_activation",
               "trailing_stop_distance", "consecutive_loss_threshold",
               "consecutive_loss_cooldown_s", "slippage_impact_factor",
               "min_fill_depth_score", "partial_fill_depth_threshold",
               "equity_snapshot_interval_s",
           ]
           if raw.get("paper_trading", {}).get(k) is not None},
        # Environment overrides
        paper_trade=os.getenv("PAPER_TRADE", "true").lower() != "false",
        env=os.getenv("ENV", "development"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        bankroll_usd=float(os.getenv("BANKROLL_USD", "1000")),
        polymarket_api_key=os.getenv("POLYMARKET_API_KEY", ""),
        polymarket_secret=os.getenv("POLYMARKET_SECRET", ""),
        polymarket_passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
        polymarket_wallet_private_key=os.getenv("POLYMARKET_WALLET_PRIVATE_KEY", ""),
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
    )
    return cfg
