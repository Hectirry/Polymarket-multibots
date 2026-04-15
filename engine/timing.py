"""
engine/timing.py — Timing-based adjustments to signal confidence and sizing.

Rules applied in probability.py and signal_router.py:
  - Very new markets (fraction < 0.10): confidence reduced (information sparse).
  - Near-expiry markets (fraction > 0.90): urgency up, sizing down.
  - Very short time-to-expiry (< 5 min): only enter if delta > 0.20 hard gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.models import MarketSnapshot

logger = logging.getLogger(__name__)


@dataclass
class TimingAdjustment:
    confidence_multiplier: float   # 0.0–1.0; applied to probability confidence
    size_multiplier: float         # 0.0–1.0; applied to Kelly sizing
    allow_entry: bool              # False → hard block regardless of delta
    reason: str


def compute_timing_adjustment(
    market: MarketSnapshot,
    delta: float,
    min_delta_near_expiry: float = 0.20,
) -> TimingAdjustment:
    """Return timing-based adjustments for a market snapshot."""

    frac = market.fraction_of_life_elapsed
    ttl = market.time_to_expiry_s

    # Hard block: less than 5 minutes left and delta too small
    if ttl < 300:
        if delta < min_delta_near_expiry:
            reason = f"TTL={ttl:.0f}s < 300s and delta={delta:.3f} < {min_delta_near_expiry}"
            logger.debug("TimingAdjustment BLOCK: %s", reason)
            return TimingAdjustment(
                confidence_multiplier=0.0,
                size_multiplier=0.0,
                allow_entry=False,
                reason=reason,
            )
        # Large delta near expiry — allow but shrink size
        return TimingAdjustment(
            confidence_multiplier=1.0,
            size_multiplier=0.5,
            allow_entry=True,
            reason="Near-expiry, large delta — small size",
        )

    # Very new market: low confidence
    if frac < 0.10:
        return TimingAdjustment(
            confidence_multiplier=0.5,
            size_multiplier=0.7,
            allow_entry=True,
            reason=f"Very new market (frac={frac:.2f}) — reduced confidence",
        )

    # Late-stage market: urgency up, size down
    if frac > 0.90:
        return TimingAdjustment(
            confidence_multiplier=1.1,
            size_multiplier=0.6,
            allow_entry=True,
            reason=f"Late-stage market (frac={frac:.2f}) — size reduced",
        )

    return TimingAdjustment(
        confidence_multiplier=1.0,
        size_multiplier=1.0,
        allow_entry=True,
        reason="Normal timing",
    )


def human_readable_ttl(seconds: float) -> str:
    """Format seconds into a human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86_400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86_400:.1f}d"
