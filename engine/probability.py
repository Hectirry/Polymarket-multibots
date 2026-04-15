"""
engine/probability.py — Core probability estimation engine.

Combines three signals:
  1. Binance spot price vs. market's implied strike to estimate binance_implied_prob.
  2. Polymarket's own market_implied_prob (best_bid of YES token).
  3. Optional whale adjustment (±0.05) when a WhaleSignal is present.

The resulting delta drives signal generation; timing adjustments are applied
separately by signal_router using engine/timing.py.

Strike extraction heuristic:
  We parse the market question text for a price threshold (e.g. "$70,000" or "70k").
  If no strike is found we fall back to treating the question purely on the market's
  implied probability and use a neutral 0.50 as our estimate (no edge asserted).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from core.models import MarketSnapshot, PriceSnapshot, Side, WhaleSignal

logger = logging.getLogger(__name__)

_PRICE_PATTERN = re.compile(
    r"\$?([\d,]+(?:\.\d+)?)\s*[kK]?\b"
)


def _parse_strike(question: str) -> Optional[float]:
    """Extract the first numeric price target from a question string."""
    # Try explicit dollar amounts with commas / k suffix
    matches = re.findall(r"\$([\d,]+(?:\.\d+)?)\s*([kK]?)", question)
    for num_str, suffix in matches:
        try:
            val = float(num_str.replace(",", ""))
            if suffix.lower() == "k":
                val *= 1000
            if val > 1:   # ignore noise like "$1"
                return val
        except ValueError:
            continue
    return None


def _spot_to_implied_prob(spot_price: float, strike: float) -> float:
    """
    Heuristic: model P(spot > strike at expiry) using a logistic function.

    This is intentionally simple — we're not pricing options, just getting a
    directional signal.  The steepness parameter σ represents uncertainty.
    """
    if strike <= 0:
        return 0.5
    ratio = spot_price / strike
    # Logistic: 1 / (1 + exp(-k * (ratio - 1)))
    k = 8.0   # controls sharpness; 8 = "50% at-the-money, tails ≈0/1 when ±30%"
    import math
    return 1.0 / (1.0 + math.exp(-k * (ratio - 1.0)))


@dataclass
class ProbabilityEstimate:
    binance_implied_prob: float     # our estimate from spot price
    market_implied_prob: float      # market's own bid (raw)
    delta: float                    # binance_prob - market_prob (before whale)
    adjusted_delta: float           # after whale adjustment
    our_prob: float                 # final probability we act on
    strike: Optional[float]         # price threshold parsed from question
    whale_adjusted: bool


def estimate(
    market: MarketSnapshot,
    price: Optional[PriceSnapshot],
    whale: Optional[WhaleSignal] = None,
) -> ProbabilityEstimate:
    """
    Compute ProbabilityEstimate for a market + current spot price.

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
        )

    binance_prob = _spot_to_implied_prob(price.price, strike)
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
    )
