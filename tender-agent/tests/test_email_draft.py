"""Suggested-reply drafting — reuses the brief LLM client, never sends.

The draft is produced from a mocked LLM (no network). The system exposes NO
send path: the EmailProvider interface has no send method and the email package
defines no send function.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tender_agent.models import Tender
from tender_agent.services.email.draft import generate_draft
from tender_agent.services.email.providers.base import EmailMessage, EmailProvider
from tests._email_fixtures import FakeLLM


def _message(subject: str = "DN12345 clarification") -> EmailMessage:
    return EmailMessage(
        id="m1",
        subject=subject,
        sender="buyer@council.gov.uk",
        received_at=datetime.now(UTC),
        body_text="Please confirm you will respond to the clarification.",
    )


def _tender() -> Tender:
    t = Tender(source_code="FTS", source_ref="DN12345", title="Cleaning services")
    t.id = 7
    t.buyer_name = "Bristol City Council"
    t.procurement_ref = None
    return t


@pytest.mark.asyncio
async def test_draft_is_produced_and_marked_drafted() -> None:
    llm = FakeLLM()
    result = await generate_draft(_message(), _tender(), llm=llm)
    assert result.status == "drafted"
    assert "respond" in result.draft_text.lower()
    # The prompt carried the tender reference + the email body for grounding.
    assert len(llm.calls) == 1
    _system, user, _max = llm.calls[0]
    assert "DN12345" in user


@pytest.mark.asyncio
async def test_informational_email_yields_no_reply_needed() -> None:
    llm = FakeLLM(
        '{"reply_needed": false, "draft": "Informational only — no reply '
        'needed.", "reasoning": "documents published notice"}'
    )
    result = await generate_draft(_message(), _tender(), llm=llm)
    assert result.status == "no_reply_needed"


@pytest.mark.asyncio
async def test_unparseable_model_output_degrades_to_error() -> None:
    result = await generate_draft(_message(), _tender(), llm=FakeLLM("not json"))
    assert result.status == "error"
    assert result.draft_text == ""


@pytest.mark.asyncio
async def test_llm_exception_does_not_raise() -> None:
    class _Boom:
        model = "x"

        async def complete(self, *, system, user, max_tokens):
            raise RuntimeError("api down")

    result = await generate_draft(_message(), _tender(), llm=_Boom())
    assert result.status == "error"


def test_no_send_capability_anywhere() -> None:
    # The provider interface has no send method...
    assert not any(
        attr for attr in dir(EmailProvider) if attr.startswith("send")
    )
    # ...and the email package exposes no send function.
    import tender_agent.services.email as email_pkg
    import tender_agent.services.email.poller as poller

    for mod in (email_pkg, poller):
        assert not any(
            name for name in dir(mod) if name.startswith("send_email")
            or name == "send"
        )
