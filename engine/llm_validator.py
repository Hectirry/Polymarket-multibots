"""
engine/llm_validator.py — Optional LLM-based signal validation via OpenRouter.

Only called when ALL of:
  1. llm_validation_enabled is True
  2. delta >= min_delta (technical signal present)
  3. ev_net_fees >= min_ev_threshold
  4. whale_pressure_score >= llm_min_whale_score
  5. Not cached within the last llm_cache_ttl_s seconds for this condition_id

Model: anthropic/claude-3-haiku (fast, cheap)
If the LLM fails or times out (>3s), the signal proceeds without validation
— the LLM is advisory, not a hard gate.

Response schema: {"validate": bool, "confidence": float, "reason": str}
Threshold to pass: validate==True AND confidence >= 0.65
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from core.config import AppConfig
from engine.timing import human_readable_ttl

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_MODEL = "anthropic/claude-3-haiku"
_TIMEOUT_S = 3.0
_CONFIDENCE_THRESHOLD = 0.65

_SYSTEM_PROMPT = (
    "You are a prediction market trading validator. "
    "Respond ONLY with valid JSON, no explanation. "
    'Schema: {"validate": bool, "confidence": float, "reason": str (max 20 words)}'
)


@dataclass
class LLMValidation:
    validated: bool
    confidence: float
    reason: str
    skipped: bool = False   # True when LLM gate conditions not met
    error: bool = False     # True when LLM call failed


class LLMValidator:
    """Calls OpenRouter to validate a trade signal with an LLM sanity check."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._cache: dict[str, float] = {}   # condition_id → last_call_ts

    def _is_cached(self, condition_id: str) -> bool:
        last = self._cache.get(condition_id)
        if last is None:
            return False
        return (time.time() - last) < self._cfg.llm_cache_ttl_s

    def _should_call(
        self,
        delta: float,
        ev_net_fees: float,
        whale_pressure_score: float,
        condition_id: str,
    ) -> bool:
        if not self._cfg.llm_validation_enabled:
            return False
        if delta < self._cfg.min_delta:
            return False
        if ev_net_fees < self._cfg.min_ev_threshold:
            return False
        if whale_pressure_score < self._cfg.llm_min_whale_score:
            return False
        if self._is_cached(condition_id):
            logger.debug("LLM cache hit for %s — skipping call", condition_id)
            return False
        return True

    async def validate(
        self,
        condition_id: str,
        question: str,
        crypto_symbol: str,
        spot_price: float,
        market_implied_prob: float,
        our_estimated_prob: float,
        delta: float,
        ev_net_fees: float,
        whale_trade_count: int,
        whale_volume_usd: float,
        whale_direction: str,
        time_remaining_s: float,
        depth_score: float,
        whale_pressure_score: float,
    ) -> LLMValidation:
        if not self._should_call(delta, ev_net_fees, whale_pressure_score, condition_id):
            return LLMValidation(validated=True, confidence=1.0, reason="skipped", skipped=True)

        if not self._cfg.openrouter_api_key:
            logger.debug("No OPENROUTER_API_KEY — skipping LLM validation")
            return LLMValidation(validated=True, confidence=1.0, reason="no_key", skipped=True)

        user_prompt = (
            f"Market: {question}\n"
            f"Crypto: {crypto_symbol} | Current price: ${spot_price:.2f}\n"
            f"Market implies: {market_implied_prob:.1%} probability YES\n"
            f"Our model: {our_estimated_prob:.1%} | Delta: {delta:+.1%}\n"
            f"Whale activity: {whale_trade_count} trades, ${whale_volume_usd:.0f} total, "
            f"direction={whale_direction}\n"
            f"Time remaining: {human_readable_ttl(time_remaining_s)}\n"
            f"Orderbook depth score: {depth_score:.2f}\n\n"
            "Should we enter this trade? Consider: is the delta real or noise?"
        )

        payload = {
            "model": _MODEL,
            "max_tokens": 150,
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
                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            content = data["choices"][0]["message"]["content"].strip()
            parsed = json.loads(content)
            validate = bool(parsed.get("validate", False))
            confidence = float(parsed.get("confidence", 0.0))
            reason = str(parsed.get("reason", ""))[:100]

            self._cache[condition_id] = time.time()

            passed = validate and confidence >= _CONFIDENCE_THRESHOLD
            if not passed:
                logger.info(
                    "LLM rejected %s: validate=%s conf=%.2f reason=%s",
                    condition_id, validate, confidence, reason,
                )
            else:
                logger.info(
                    "LLM validated %s: conf=%.2f reason=%s",
                    condition_id, confidence, reason,
                )
            return LLMValidation(validated=passed, confidence=confidence, reason=reason)

        except Exception as exc:
            logger.warning("LLM validation failed for %s (%s) — proceeding without", condition_id, exc)
            return LLMValidation(validated=True, confidence=0.0, reason=str(exc), error=True)


# asyncio needed for TimeoutError reference
import asyncio  # noqa: E402
