"""
engine/market_ranker.py — LLM-based pre-filter market attractiveness scorer.

Runs BEFORE the signal_router pipeline on markets that have already passed
the feed's liquidity/timing filters. Scores each market 0.0-1.0 on how
attractive it is to trade right now, conditioning on a PRE-COMPUTED
probability estimate (from engine.probability) so the LLM does not have to
re-derive edge from the question text.

The prompt is dense: every field is something the model needs to decide.
It explicitly reminds the model of the 2% round-trip fee so the rubric
penalises marginal edges.

Uses OpenRouter. On API error, timeout, or missing key, the ranker
returns score=None and the pipeline treats it as neutral (no veto).
Results are cached per condition_id for `ranker_cache_ttl_s` seconds.
Cumulative OpenRouter usage cost is tracked on the instance for cost
monitoring.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from core.config import AppConfig
from core.models import MarketSnapshot, PriceSnapshot, Side, WhaleSignal
from engine import probability
from engine.probability import ProbabilityEstimate
from engine.timing import human_readable_ttl

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM_PROMPT = (
    "You rank crypto prediction markets on Polymarket for a paper-trading bot.\n"
    "The bot pays ~2% round-trip fees, so a market is only tradeable if we have\n"
    "CLEAR edge AFTER fees. Cheap talk is not edge. Respond with valid JSON only.\n"
    "\n"
    'Schema: {"score": float 0.0-1.0, "reason": str (max 20 words, cite the decisive factor)}\n'
    "\n"
    "Scoring rubric (buckets are load-bearing — calibrate to them):\n"
    "  0.00-0.29  SKIP: no real edge, near-certain outcome, thin book, or no catalyst\n"
    "  0.30-0.49  WEAK: some edge but 2% fees eat it, or whale flow contradicts our model\n"
    "  0.50-0.69  TRADEABLE: positive net-of-fees edge, clean setup, coherent flow\n"
    "  0.70-1.00  STRONG: large edge, coherent whale flow, liquid, clear near-term catalyst\n"
    "\n"
    "Auto-reject (score < 0.3) when ANY of:\n"
    "  - Market price > 95% or < 5% (almost no room for edge)\n"
    "  - Required price move is unrealistic for the time remaining\n"
    "  - No whale flow AND our model edge is below 3% (noise)\n"
    "  - Whale flow contradicts our model edge direction\n"
    "  - Book too thin (low OI or wide spread) to exit cleanly\n"
    "\n"
    "Be concise. This is a high-frequency filter — do not over-reason."
)


@dataclass
class RankResult:
    score: Optional[float]      # None → error or not called; treated as neutral
    reason: str
    skipped: bool = False       # True when gate conditions not met (disabled / no key)
    error: bool = False         # True when the HTTP call failed
    from_cache: bool = False    # True when result came from the in-memory cache
    cost_usd: float = 0.0       # OpenRouter usage.cost for this call (0 if cached/skipped)


def _whale_agrees(our_edge: float, whale_dir: str) -> Optional[bool]:
    """Does whale direction agree with our model's edge sign?"""
    if abs(our_edge) < 0.02 or whale_dir in ("NONE", ""):
        return None
    if our_edge > 0 and whale_dir == "YES":
        return True
    if our_edge < 0 and whale_dir == "NO":
        return True
    return False


class MarketRanker:
    """Pre-filter LLM scorer for market attractiveness."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._cache: dict[str, tuple[float, RankResult]] = {}
        self.total_cost_usd: float = 0.0
        self.total_api_calls: int = 0
        self.cache_hits: int = 0

    def stats(self) -> dict:
        """Return a snapshot of ranker health/cost metrics for the dashboard."""
        now = time.time()
        live_cache = sum(
            1 for ts, _ in self._cache.values()
            if (now - ts) < self._cfg.ranker_cache_ttl_s
        )
        total_lookups = self.total_api_calls + self.cache_hits
        hit_rate = (self.cache_hits / total_lookups) if total_lookups > 0 else 0.0
        return {
            "enabled": self._cfg.ranker_enabled,
            "model": self._cfg.ranker_model,
            "min_score": self._cfg.ranker_min_score,
            "total_api_calls": self.total_api_calls,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "cache_size": live_cache,
            "cache_hits": self.cache_hits,
            "cache_hit_rate": round(hit_rate, 3),
            "avg_cost_per_call": round(
                self.total_cost_usd / self.total_api_calls, 4,
            ) if self.total_api_calls > 0 else 0.0,
        }

    def _cached(self, condition_id: str) -> Optional[RankResult]:
        entry = self._cache.get(condition_id)
        if entry is None:
            return None
        ts, result = entry
        if (time.time() - ts) >= self._cfg.ranker_cache_ttl_s:
            return None
        return result

    async def rank(
        self,
        market: MarketSnapshot,
        price: Optional[PriceSnapshot],
        whale: Optional[WhaleSignal],
        prob_est: Optional[ProbabilityEstimate] = None,
    ) -> RankResult:
        if not self._cfg.ranker_enabled:
            return RankResult(score=None, reason="disabled", skipped=True)
        if not self._cfg.openrouter_api_key:
            return RankResult(score=None, reason="no_key", skipped=True)

        cached = self._cached(market.condition_id)
        if cached is not None:
            self.cache_hits += 1
            logger.debug("Ranker cache hit for %s", market.condition_id)
            return RankResult(
                score=cached.score, reason=cached.reason,
                from_cache=True, cost_usd=0.0,
            )

        # Pre-compute probability estimate if not supplied.
        if prob_est is None:
            prob_est = probability.estimate(market, price, whale)

        spot = price.price if price else 0.0
        whale_n = whale.trade_count if whale else 0
        whale_vol = whale.total_volume_usd if whale else 0.0
        whale_dir = whale.direction.value if whale else "NONE"

        # Derived fields the LLM needs
        strike_str = f"${prob_est.strike:,.0f}" if prob_est.strike else "unknown"
        if prob_est.strike and spot:
            req_move_pct = (prob_est.strike - spot) / spot
            move_str = f"{req_move_pct:+.2%}"
        else:
            move_str = "n/a"

        our_edge = prob_est.adjusted_delta           # our_prob - market_prob (after whale)
        if our_edge >= 0:
            side = "YES"
            entry = market.best_ask
        else:
            side = "NO"
            entry = 1.0 - market.best_bid

        agrees = _whale_agrees(our_edge, whale_dir)
        agrees_str = (
            "yes" if agrees is True
            else ("no" if agrees is False else "n/a")
        )

        user_prompt = (
            f"Market: {market.question}\n"
            f"Symbol: {market.crypto_symbol}  |  Spot: ${spot:,.2f}  "
            f"|  Strike: {strike_str}  |  Required move: {move_str}\n"
            f"Time to expiry: {human_readable_ttl(market.time_to_expiry_s)}  "
            f"|  Life elapsed: {market.fraction_of_life_elapsed:.0%}\n"
            f"Market P(YES): {prob_est.market_implied_prob:.1%}  "
            f"|  Our model P: {prob_est.our_prob:.1%}  "
            f"|  Edge (ours-market): {our_edge:+.1%}\n"
            f"Preferred side: {side}  |  Entry price: {entry:.3f}  "
            f"(need > {entry + 0.02:.3f} net of 2% fees to be EV+)\n"
            f"Liquidity: 24h vol ${market.volume_24h:,.0f}  "
            f"|  OI ${market.open_interest:,.0f}  |  Spread {market.spread:.3f}\n"
            f"Whale flow: {whale_n} trades, ${whale_vol:,.0f}, direction {whale_dir}  "
            f"|  Agrees with our edge: {agrees_str}"
        )

        # MiniMax M2.7 and other reasoning models consume tokens on internal
        # chain-of-thought even when `exclude:true` strips reasoning from the
        # response — the tokens still count against max_tokens. Empirically
        # reasoning on this prompt ranges 500-900 tokens, so 2048 gives headroom
        # for the final JSON to always fit. (You only pay for tokens actually
        # used, not the cap.)
        payload = {
            "model": self._cfg.ranker_model,
            "max_tokens": 2048,
            "reasoning": {"exclude": True},
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._cfg.openrouter_api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _OPENROUTER_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self._cfg.ranker_timeout_s),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content.strip():
                content = msg.get("reasoning") or ""
            content = content.strip()
            if not content:
                raise ValueError(f"empty content from {self._cfg.ranker_model}")
            if content.startswith("```"):
                content = content.strip("`").lstrip("json").strip()
            start = content.find("{")
            if start > 0:
                content = content[start:]
            parsed, _end = json.JSONDecoder().raw_decode(content)
            score = float(parsed.get("score", 0.5))
            score = max(0.0, min(1.0, score))
            reason = str(parsed.get("reason", ""))[:150]

            cost = float(data.get("usage", {}).get("cost", 0.0) or 0.0)
            self.total_cost_usd += cost
            self.total_api_calls += 1

            result = RankResult(
                score=score, reason=reason, from_cache=False, cost_usd=cost,
            )
            self._cache[market.condition_id] = (time.time(), result)
            logger.info(
                "RANKER %s score=%.2f cost=$%.4f total=$%.4f reason=%s",
                market.condition_id, score, cost, self.total_cost_usd, reason,
            )
            return result

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            logger.warning(
                "Ranker failed for %s (%s) — passing through neutral",
                market.condition_id, err,
            )
            self.total_api_calls += 1     # an HTTP attempt was still made
            return RankResult(
                score=None, reason=err[:150], error=True, from_cache=False,
            )
