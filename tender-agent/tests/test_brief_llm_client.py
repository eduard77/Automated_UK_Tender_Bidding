"""LLM client wrapper — config + error surface (chunk 5).

Never hits a real API. Verifies that missing-key raises LLMConfigurationError
with the actionable message, and that the model default constant is exposed.
"""
from __future__ import annotations

import pytest

from tender_agent.services.brief.llm_client import (
    BRIEF_LLM_MODEL_DEFAULT,
    AnthropicBriefLLMClient,
    LLMConfigurationError,
)


def test_default_model_is_named_constant() -> None:
    # Single named constant — protects against drift / typos elsewhere.
    assert BRIEF_LLM_MODEL_DEFAULT == "claude-sonnet-4-6"
    client = AnthropicBriefLLMClient(api_key="xx")
    assert client.model == BRIEF_LLM_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_missing_api_key_raises_actionable_error(monkeypatch) -> None:
    # Both env var AND settings.anthropic_api_key absent.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from tender_agent import config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "anthropic_api_key", "")

    client = AnthropicBriefLLMClient(api_key=None)
    with pytest.raises(LLMConfigurationError) as exc:
        await client.complete(system="s", user="u", max_tokens=10)
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert ".env" in str(exc.value)


def test_brief_model_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BRIEF_LLM_MODEL", "claude-haiku-test")
    client = AnthropicBriefLLMClient(api_key="xx")
    assert client.model == "claude-haiku-test"
