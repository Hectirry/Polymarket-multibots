"""
execution/slippage.py — Realistic paper-trade fill simulation.

Models three real effects that flat-fee simulators ignore:

1. Market impact slippage
   Larger orders vs. thin books move the fill price against you.
   slippage = size_usd / (open_interest * impact_factor)
   where impact_factor ≈ 0.002 means a $200 order in a $100k OI market
   adds 0.4% slippage on top of the fee.

2. Fill rejection
   If depth_score < min_fill_depth_score (book is almost empty), the order
   is rejected outright — no fill.  Simulates a real market where your limit
   order never matches.

3. Partial fills
   If depth_score < partial_fill_depth_threshold, only a fraction of the
   order fills (proportional to depth).  The rest is left unfilled and the
   size_usd is scaled accordingly.

Exit fills use a separate model: selling into a thin book is worse than buying
because you're a price-taker crossing the spread in the opposite direction.

All parameters come from AppConfig (paper_trading section of config.json).
"""

from __future__ import annotations

import logging
import random

from core.config import AppConfig
from core.models import FillResult

logger = logging.getLogger(__name__)

_POLYMARKET_FEE = 0.02


def simulate_entry_fill(
    requested_price: float,
    size_usd: float,
    open_interest: float,
    depth_score: float,
    cfg: AppConfig,
) -> FillResult:
    """
    Simulate a paper entry fill with market impact and depth checks.

    Args:
        requested_price:  Signal's entry_price (best_ask for YES).
        size_usd:         Dollar amount of the order.
        open_interest:    Market open interest in USDC (depth proxy).
        depth_score:      Orderbook depth score 0–1 from OrderbookAnalyzer.
        cfg:              AppConfig for tuning parameters.

    Returns:
        FillResult with fill details. If filled=False caller must abort the trade.
    """
    # ── Reject: book too thin ──────────────────────────────────────────────────
    if depth_score < cfg.min_fill_depth_score:
        logger.info(
            "Fill REJECTED: depth_score=%.3f < min=%.3f (size=$%.2f)",
            depth_score, cfg.min_fill_depth_score, size_usd,
        )
        return FillResult(
            filled=False,
            fill_price=requested_price,
            requested_price=requested_price,
            slippage=0.0,
            fill_fraction=0.0,
            actual_size_usd=0.0,
            fee_usd=0.0,
        )

    # ── Partial fill: shallow book ─────────────────────────────────────────────
    fill_fraction = 1.0
    if depth_score < cfg.partial_fill_depth_threshold:
        # Fill fraction scales linearly with depth: 0 at min, 1 at partial threshold
        fill_fraction = max(0.2, depth_score / cfg.partial_fill_depth_threshold)
        logger.debug(
            "Partial fill: depth_score=%.3f → fill_fraction=%.2f",
            depth_score, fill_fraction,
        )

    actual_size = size_usd * fill_fraction

    # ── Market impact slippage ────────────────────────────────────────────────
    # Larger order relative to OI → worse price
    reference_depth = max(open_interest, 1_000)   # floor to avoid div/0
    impact = (actual_size / reference_depth) * cfg.slippage_impact_factor * 10
    # Cap slippage at 3% (extreme case)
    impact = min(impact, 0.03)

    fill_price = min(requested_price + impact, 0.99)
    slippage = fill_price - requested_price

    fee_usd = actual_size * _POLYMARKET_FEE

    logger.debug(
        "Entry fill: req=%.4f fill=%.4f slippage=%.4f (%.2f%%) "
        "size=$%.2f fill_frac=%.2f fee=$%.2f",
        requested_price, fill_price, slippage, slippage * 100,
        actual_size, fill_fraction, fee_usd,
    )
    return FillResult(
        filled=True,
        fill_price=fill_price,
        requested_price=requested_price,
        slippage=slippage,
        fill_fraction=fill_fraction,
        actual_size_usd=actual_size,
        fee_usd=fee_usd,
    )


def simulate_exit_fill(
    exit_price: float,
    trade_size_usd: float,
    entry_price: float,
    depth_score: float,
    cfg: AppConfig,
) -> tuple[float, float]:
    """
    Simulate exit fill price and fee.

    Exits are typically worse than entries — selling into a thin book
    means crossing a wider spread.  We add half the entry slippage model
    in the adverse direction.

    Returns:
        (net_exit_price, fee_usd)
    """
    contracts = trade_size_usd / entry_price if entry_price > 0 else 0

    # Exit slippage: proportional to depth (worse = less depth)
    thin_book_penalty = max(0.0, 1.0 - depth_score) * 0.01   # up to 1% penalty
    actual_exit = max(0.0, exit_price - thin_book_penalty)

    payout = contracts * actual_exit
    fee_usd = payout * _POLYMARKET_FEE

    logger.debug(
        "Exit fill: raw=%.4f actual=%.4f penalty=%.4f fee=$%.2f",
        exit_price, actual_exit, thin_book_penalty, fee_usd,
    )
    return actual_exit, fee_usd
