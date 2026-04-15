"""
engine/ev_calculator.py — Expected value calculation with Polymarket fee model.

Polymarket charges ~2% on the notional of each trade.  EV must clear the fee
hurdle before a signal is actionable.

Formula:
    ev_gross = our_prob * (1 - entry_price) - (1 - our_prob) * entry_price
    fee       = 0.02 * entry_price           # 2% of notional on entry
    ev_net    = ev_gross - fee
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

POLYMARKET_FEE_RATE = 0.02  # 2% of notional


@dataclass
class EVResult:
    ev_gross: float
    ev_net_fees: float
    fee_cost: float
    breakeven_prob: float   # minimum our_prob to be +EV after fees


def calculate_ev(
    our_prob: float,
    entry_price: float,
    fee_rate: float = POLYMARKET_FEE_RATE,
) -> EVResult:
    """
    Calculate expected value for buying YES at entry_price, given our_prob.

    Args:
        our_prob:    Our estimated probability that the event resolves YES.
        entry_price: Price of YES token (0.0–1.0).
        fee_rate:    Fraction of entry_price charged as fee (default 2%).

    Returns:
        EVResult with gross and net-of-fees expected values.
    """
    if not (0.0 < entry_price < 1.0):
        return EVResult(
            ev_gross=0.0,
            ev_net_fees=-fee_rate,
            fee_cost=fee_rate * entry_price,
            breakeven_prob=entry_price + fee_rate,
        )

    # Profit if YES resolves = (1 - entry_price) per dollar spent
    # Loss if NO resolves   = entry_price per dollar spent
    ev_gross = our_prob * (1.0 - entry_price) - (1.0 - our_prob) * entry_price
    fee_cost = fee_rate * entry_price
    ev_net = ev_gross - fee_cost

    # Breakeven: solve ev_gross = fee_cost
    # our_prob * (1 - p) - (1 - our_prob) * p = fee_rate * p
    # → our_prob = p * (1 + fee_rate) / (1 + p * fee_rate - p + p)  [simplified]
    if (1.0 - entry_price + entry_price) > 0:
        breakeven_prob = (entry_price + fee_cost) / 1.0
    else:
        breakeven_prob = entry_price

    logger.debug(
        "EV calc: our_prob=%.3f entry=%.3f ev_gross=%.4f fee=%.4f ev_net=%.4f",
        our_prob, entry_price, ev_gross, fee_cost, ev_net,
    )
    return EVResult(
        ev_gross=ev_gross,
        ev_net_fees=ev_net,
        fee_cost=fee_cost,
        breakeven_prob=breakeven_prob,
    )


def kelly_size(
    our_prob: float,
    entry_price: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
    max_position_pct: float = 0.04,
) -> float:
    """
    Fractional Kelly criterion position sizing.

    Returns the dollar size to bet, capped at max_position_pct of bankroll.
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    b = (1.0 - entry_price) / entry_price   # net odds per dollar
    q = 1.0 - our_prob
    # Kelly formula: f* = (p*b - q) / b
    kelly_f = (our_prob * b - q) / b if b > 0 else 0.0
    kelly_f = max(0.0, kelly_f)
    fractional = kelly_f * kelly_fraction
    max_size = bankroll * max_position_pct
    size = min(fractional * bankroll, max_size)
    logger.debug(
        "Kelly: p=%.3f b=%.3f kelly_f=%.3f frac=%.3f size=$%.2f (cap=$%.2f)",
        our_prob, b, kelly_f, fractional, size, max_size,
    )
    return round(size, 2)
