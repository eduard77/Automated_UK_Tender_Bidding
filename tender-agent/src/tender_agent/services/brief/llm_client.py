"""Thin async wrapper around the Anthropic Messages API for brief generation.

Designed to be cheap to mock: tests inject a `FakeLLMClient` (or any object
with the same `generate_brief` signature) and never make a real API call. The
production implementation lives in `AnthropicBriefClient`; the active
implementation is chosen by `get_default_client()` from env config.

Configuration:
- ANTHROPIC_API_KEY — required at call time. Missing raises a clear
  `LLMConfigError` (not a 500) so the API layer can return a useful message.
- BRIEF_LLM_MODEL — model id. Defaults to DEFAULT_BRIEF_MODEL below.

The constant `DEFAULT_BRIEF_MODEL` is the single named place this lives so
the model can be bumped in one edit. It is intentionally a sonnet model —
the brief is a reasoning task and we want quality over latency.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


# Single named constant. Override via env BRIEF_LLM_MODEL if needed.
DEFAULT_BRIEF_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_OUTPUT_TOKENS = 4096


class LLMConfigError(RuntimeError):
    """Configuration problem the operator can fix (missing key, bad model)."""


class LLMCallError(RuntimeError):
    """The API was called but failed (timeout, server error, etc.)."""


@dataclass
class LLMResponse:
    """The raw text Claude returned plus token usage for accounting."""

    text: str
    input_tokens: int | None
    output_tokens: int | None
    model: str


class BriefLLMClient(Protocol):
    """Anything that can take a (system, user) prompt pair and return text.

    The brief generator depends on this protocol so tests can pass a fake
    that returns canned JSON, and CI never makes a real API call.
    """

    async def generate_brief(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> LLMResponse:
        ...


class AnthropicBriefClient:
    """Production client. Single retry on transient errors, then fail."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str | None = None,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model or os.environ.get(
            "BRIEF_LLM_MODEL", DEFAULT_BRIEF_MODEL
        )
        self._timeout_seconds = timeout_seconds

    def _resolve_key(self) -> str:
        # Read env at call time so tests / restarts pick up changes without
        # re-importing the module.
        key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            # Fall back to the existing pydantic settings (other code in the
            # repo already wires ANTHROPIC_API_KEY there).
            try:
                from tender_agent.config import settings

                key = settings.anthropic_api_key or None
            except Exception:  # noqa: BLE001
                key = None
        if not key:
            raise LLMConfigError(
                "ANTHROPIC_API_KEY not set — add it to the backend .env to "
                "generate briefs."
            )
        return key

    async def generate_brief(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> LLMResponse:
        api_key = self._resolve_key()
        model = model or self._default_model

        last_exc: Exception | None = None
        # One retry on transient failure, then surface a clean error.
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(
                    self._call_anthropic(
                        api_key=api_key,
                        system=system,
                        user=user,
                        model=model,
                        max_output_tokens=max_output_tokens,
                    ),
                    timeout=self._timeout_seconds,
                )
            except LLMConfigError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "brief.llm_call_attempt_failed",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt == 2:
                    break
                await asyncio.sleep(1.0)
        raise LLMCallError(
            f"Anthropic API call failed after retry: {last_exc}"
        ) from last_exc

    async def _call_anthropic(
        self,
        *,
        api_key: str,
        system: str,
        user: str,
        model: str,
        max_output_tokens: int,
    ) -> LLMResponse:
        # Local import so the rest of the codebase doesn't pull anthropic at
        # import time. The SDK is already in pyproject.
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=api_key)
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        finally:
            # AsyncAnthropic uses httpx underneath — best-effort close.
            with contextlib.suppress(Exception):
                await client.close()

        text_blocks = [
            getattr(b, "text", "")
            for b in resp.content
            if getattr(b, "type", None) == "text"
        ]
        text = "".join(text_blocks)
        usage = getattr(resp, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage else None
        output_tokens = getattr(usage, "output_tokens", None) if usage else None
        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )


def get_default_client() -> BriefLLMClient:
    """The active client. Production code calls this. Tests inject a fake
    explicitly instead of using the default."""
    return AnthropicBriefClient()


# --------------------------------------------------------------------------
# Test double — exported so tests can `from .llm_client import FakeLLMClient`.
# --------------------------------------------------------------------------


class FakeLLMClient:
    """Returns canned text for every call. Use in tests so CI never burns
    API credit. Pass a list of responses to simulate retry behaviour."""

    def __init__(
        self,
        responses: list[str] | str,
        *,
        model: str = "fake-model",
        input_tokens: int = 1000,
        output_tokens: int = 800,
    ) -> None:
        if isinstance(responses, str):
            responses = [responses]
        self._responses = list(responses)
        self._model = model
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.calls: list[dict] = []

    async def generate_brief(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "model": model or self._model,
                "max_output_tokens": max_output_tokens,
            }
        )
        if not self._responses:
            raise LLMCallError("FakeLLMClient ran out of canned responses")
        text = self._responses.pop(0)
        return LLMResponse(
            text=text,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            model=model or self._model,
        )
