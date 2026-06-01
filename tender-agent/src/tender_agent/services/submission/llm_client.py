"""Drafting LLM client.

The drafting agent is injectable — production hits Anthropic, tests pass a
fake that returns canned schema-valid JSON. The wire shape is the same as
the brief engine's (system + user + max_tokens → text), and we deliberately
re-use the brief engine's environment plumbing (`ANTHROPIC_API_KEY`,
`BRIEF_LLM_MODEL` for the default model family) so the operator only has to
configure one place. A second env var (`SUBMISSION_LLM_MODEL`) overrides per
deployment without touching the brief.

ZERO real API calls in CI is a hard rule — every test uses a FakeDraftingLLM
implementation of `DraftingLLMClient`.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Protocol

import structlog

from tender_agent.services.brief.llm_client import (
    BRIEF_LLM_MODEL_DEFAULT,
    LLMConfigurationError,
)

logger = structlog.get_logger(__name__)

# Single named default; override via SUBMISSION_LLM_MODEL. Defaults to the
# same Sonnet model family the brief engine uses.
SUBMISSION_LLM_MODEL_DEFAULT = BRIEF_LLM_MODEL_DEFAULT

# Token budget for the prompt. Vault candidates + buyer context are pruned to
# fit; the engine records what was included / truncated / omitted.
SUBMISSION_PROMPT_BUDGET_TOKENS = 80_000


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model: str


class DraftingLLMClient(Protocol):
    """Anything that can take a system prompt + user prompt + max_tokens and
    produce text. Production: AnthropicDraftingLLMClient. Tests: in-memory
    fakes returning canned JSON."""

    model: str

    async def complete(
        self, *, system: str, user: str, max_tokens: int
    ) -> LLMResponse: ...


def _resolve_model() -> str:
    return (
        os.environ.get("SUBMISSION_LLM_MODEL")
        or os.environ.get("BRIEF_LLM_MODEL")
        or SUBMISSION_LLM_MODEL_DEFAULT
    )


def _resolve_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        from tender_agent.config import settings

        return settings.anthropic_api_key or None
    except Exception:  # noqa: BLE001
        return None


class AnthropicDraftingLLMClient:
    """Production client. One retry on a transient error, then a clean
    failure. Sync calls are wrapped in `asyncio.to_thread` so FastAPI's
    background task stays cooperative."""

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
                "draft submission responses"
            )
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
                    "submission.llm_call_error",
                    attempt=attempt,
                    model=self.model,
                    error=str(exc),
                )
                continue
            text_blocks = [
                b.text for b in response.content
                if getattr(b, "type", None) == "text"
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
            f"Submission LLM call failed after {self._max_retries + 1} "
            f"attempts: {last_exc}"
        )
