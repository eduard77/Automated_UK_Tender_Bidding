"""LLM client wrapper for the brief engine (chunk 5).

A thin async layer over the Anthropic Messages API. The point of this module
is that the brief generator is *injectable* — tests construct a fake client
that returns canned JSON, so no real API call ever runs in CI and the build
costs zero.

Config:
  ANTHROPIC_API_KEY   — required at call time; clear error if missing.
  BRIEF_LLM_MODEL     — optional override of the model id.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)

# Single named default for the brief LLM. Override per-deployment via the
# BRIEF_LLM_MODEL env var; tests override at call sites.
BRIEF_LLM_MODEL_DEFAULT = "claude-sonnet-4-6"


class LLMConfigurationError(RuntimeError):
    """Raised when the LLM client is asked to make a call but isn't configured
    (e.g. the API key is missing). Surfaces as a clean failure on the brief —
    never propagates a raw KeyError or auth-style exception to the API."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model: str


class BriefLLMClient(Protocol):
    """Anything that can take (system, user, max_tokens) and produce text.

    Implementations:
      AnthropicBriefLLMClient — production, hits the Anthropic API.
      Test fakes — return canned JSON, never touch the network.
    """

    model: str

    async def complete(
        self, *, system: str, user: str, max_tokens: int
    ) -> LLMResponse: ...


def _resolve_model() -> str:
    return os.environ.get("BRIEF_LLM_MODEL") or BRIEF_LLM_MODEL_DEFAULT


def _resolve_api_key() -> str | None:
    # Prefer the dedicated env var; fall back to whatever the app config has
    # (so a user with ANTHROPIC_API_KEY only in the backend .env still works).
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        from tender_agent.config import settings

        return settings.anthropic_api_key or None
    except Exception:  # noqa: BLE001
        return None


class AnthropicBriefLLMClient:
    """Production client. One retry on transient error, then a clean failure.

    Synchronous calls to anthropic.Anthropic.messages.create are wrapped in
    asyncio.to_thread so the FastAPI background task stays cooperative.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        max_retries: int = 1,
    ) -> None:
        self.model = model or _resolve_model()
        self._api_key = api_key or _resolve_api_key()
        self._max_retries = max_retries

    async def complete(
        self, *, system: str, user: str, max_tokens: int
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMConfigurationError(
                "ANTHROPIC_API_KEY not set — add it to the backend .env to "
                "generate briefs"
            )

        # Imported lazily so test environments without `anthropic` available
        # still import this module fine (CI uses the fake client).
        from anthropic import Anthropic

        client = Anthropic(api_key=self._api_key)
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await asyncio.to_thread(
                    client.messages.create,
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "brief.llm_call_error",
                    attempt=attempt,
                    model=self.model,
                    error=str(exc),
                )
                continue
            text_blocks = [
                b.text for b in response.content if getattr(b, "type", None) == "text"
            ]
            text = "\n".join(text_blocks).strip()
            usage = getattr(response, "usage", None)
            return LLMResponse(
                text=text,
                input_tokens=getattr(usage, "input_tokens", None) if usage else None,
                output_tokens=(
                    getattr(usage, "output_tokens", None) if usage else None
                ),
                model=self.model,
            )
        raise RuntimeError(
            f"LLM call failed after {self._max_retries + 1} attempts: {last_exc}"
        )
