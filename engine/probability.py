"""
engine/probability.py — Core probability estimation engine.

Combines three signals:
  1. Binance spot price + realized volatility → Black-Scholes P(S_T > K).
     If volatility is unknown, falls back to a fixed-steepness logistic.
  2. Polymarket's own market_implied_prob (best_bid of YES token).
  3. Optional whale adjustment (±0.05) when a WhaleSignal is present.

The Black-Scholes path scales correctly with time-to-expiry: a 1% move 12h
from expiry is meaningful, the same move 30 days from expiry is noise.
The old logistic treated all horizons identically and systematically
underweighted short-dated markets — that's why edges looked flat.

Strike extraction:
  We parse the market question text for a price threshold (e.g. "$70,000").
  Without a strike we treat the market as neutral (our_prob = market_prob).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

from core.models import MarketSnapshot, PriceSnapshot, Side, WhaleSignal

logger = logging.getLogger(__name__)

_SECONDS_PER_YEAR = 365.25 * 24 * 3600
_LEGACY_LOGISTIC_K = 8.0


def _parse_strike(question: str) -> Optional[float]:
    """Extract the first numeric price target from a question string."""
    matches = re.findall(r"\$([\d,]+(?:\.\d+)?)\s*([kK]?)", question)
    for num_str, suffix in matches:
        try:
            val = float(num_str.replace(",", ""))
            if suffix.lower() == "k":
                val *= 1000
            if val > 1:
                return val
        except ValueError:
            continue
    return None


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_prob_above_strike(
    spot: float, strike: float, sigma_annual: float, time_to_expiry_s: float,
) -> float:
    """
    P(S_T > K) under GBM with no drift:
        d2 = (ln(S/K) - 0.5 σ² T) / (σ √T)
        P  = N(d2)

    Returns 0.5 on degenerate inputs so the caller degrades to the market's
    own implied probability rather than producing a fake extreme.
    """
    if spot <= 0 or strike <= 0 or sigma_annual <= 0 or time_to_expiry_s <= 0:
        return 0.5
    T = time_to_expiry_s / _SECONDS_PER_YEAR
    sigma_sqrt_T = sigma_annual * math.sqrt(T)
    if sigma_sqrt_T <= 1e-9:
        return 1.0 if spot > strike else (0.0 if spot < strike else 0.5)
    d2 = (math.log(spot / strike) - 0.5 * sigma_annual ** 2 * T) / sigma_sqrt_T
    return max(0.005, min(0.995, _norm_cdf(d2)))


def _logistic_prob_above_strike(spot: float, strike: float) -> float:
    """Legacy logistic fallback — time-independent, kept for backward-compat."""
    if strike <= 0:
        return 0.5
    ratio = spot / strike
    return 1.0 / (1.0 + math.exp(-_LEGACY_LOGISTIC_K * (ratio - 1.0)))


@dataclass
class ProbabilityEstimate:
    binance_implied_prob: float     # our estimate from spot price
    market_implied_prob: float      # market's own bid (raw)
    delta: float                    # binance_prob - market_prob (before whale)
    adjusted_delta: float           # after whale adjustment
    our_prob: float                 # final probability we act on
    strike: Optional[float]         # price threshold parsed from question
    whale_adjusted: bool
    using_bs: bool = False          # True when BS was used (vol available)
    sigma_annual: Optional[float] = None   # volatility used (if any)


def estimate(
    market: MarketSnapshot,
    price: Optional[PriceSnapshot],
    whale: Optional[WhaleSignal] = None,
    volatility: Optional[float] = None,
) -> ProbabilityEstimate:
    """
    Compute ProbabilityEstimate for a market + current spot price.

    When `volatility` (annualized sigma) is supplied, we use Black-Scholes
    P(S_T > K) scaled by the market's time-to-expiry — the correct way to
    compare a 12h market against a 30-day market. When volatility is None,
    we fall back to the legacy time-independent logistic.

    If spot price is unavailable or strike cannot be parsed, returns a
    neutral estimate (our_prob == market_prob, delta == 0).
    """
    market_implied_prob = market.implied_prob
    strike = _parse_strike(market.question)

    if price is None or strike is None:
        logger.debug(
            "No spot or no strike for %s (strike=%s price=%s) — neutral estimate",
            market.condition_id, strike, price,
        )
        return ProbabilityEstimate(
            binance_implied_prob=market_implied_prob,
            market_implied_prob=market_implied_prob,
            delta=0.0,
            adjusted_delta=0.0,
            our_prob=market_implied_prob,
            strike=strike,
            whale_adjusted=False,
            using_bs=False,
            sigma_annual=None,
        )

    using_bs = volatility is not None and volatility > 0 and market.time_to_expiry_s > 0
    if using_bs:
        binance_prob = _bs_prob_above_strike(
            price.price, strike, volatility, market.time_to_expiry_s,
        )
    else:
        binance_prob = _logistic_prob_above_strike(price.price, strike)
    raw_delta = binance_prob - market_implied_prob

    # Whale adjustment: ±0.05 in whale's direction
    whale_adj = 0.0
    whale_adjusted = False
    if whale is not None:
        whale_adj = 0.05 if whale.direction == Side.YES else -0.05
        whale_adjusted = True
        logger.debug(
            "Whale adjustment %+.3f for %s (direction=%s, pressure=%.2f)",
            whale_adj, market.condition_id, whale.direction, whale.pressure_score,
        )

    adjusted_delta = raw_delta + whale_adj
    our_prob = market_implied_prob + adjusted_delta  # = binance_prob + whale_adj
    our_prob = max(0.01, min(0.99, our_prob))

    logger.debug(
        "ProbEst %s: spot=$%.2f strike=$%.2f binance_p=%.3f market_p=%.3f "
        "delta=%+.3f whale_adj=%+.3f our_prob=%.3f",
        market.condition_id, price.price, strike,
        binance_prob, market_implied_prob, raw_delta, whale_adj, our_prob,
    )

    return ProbabilityEstimate(
        binance_implied_prob=binance_prob,
        market_implied_prob=market_implied_prob,
        delta=raw_delta,
        adjusted_delta=adjusted_delta,
        our_prob=our_prob,
        strike=strike,
        whale_adjusted=whale_adjusted,
        using_bs=using_bs,
        sigma_annual=volatility if using_bs else None,
    )
