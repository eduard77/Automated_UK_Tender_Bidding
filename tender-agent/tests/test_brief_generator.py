"""Bid-brief generator (Phase 4 chunk 5) — mocked LLM, zero API cost.

Covers:
- happy path with canned valid JSON → TenderBrief row populated;
- malformed JSON → one stricter retry → success;
- malformed JSON twice → clean failure (no broken brief saved);
- missing API key surfaces a clear error via the production client;
- token budgeting prioritises ITT/quality/spec and truncates spreadsheets,
  with `documents_considered` recording included/truncated/omitted;
- no-documents state.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import (
    Tender,
    TenderDocumentContent,
    TenderDocumentFile,
)
from tender_agent.services.brief import brief_generator as bg_module
from tender_agent.services.brief.brief_generator import (
    BriefPayload,
    _select_documents,
    generate_brief,
)
from tender_agent.services.brief.document_extractor import EXTRACTOR_VERSION
from tender_agent.services.brief.llm_client import (
    AnthropicBriefClient,
    FakeLLMClient,
    LLMConfigError,
)

VALID_BRIEF_JSON = json.dumps(
    {
        "recommendation": "bid",
        "confidence": "medium",
        "headline": "Strong scope-fit; tight deadline manageable.",
        "rationale": (
            "The scope sits within our delivery footprint, the buyer has "
            "shortlisted SMEs before, and the price/quality split favours "
            "method statement quality where we score well."
        ),
        "key_risks": [
            {
                "risk": "Performance bond at 10% may stretch cashflow",
                "severity": "high",
                "detail": "Bond required for the whole 3-year term.",
            },
            {
                "risk": "TUPE applies to 14 staff",
                "severity": "medium",
                "detail": "ELI data shared on request.",
            },
        ],
        "deadline": {"date": "2026-06-30", "note": "Bid + presentations same day."},
        "contract_value": {"amount": "£2.4m", "note": "estimated over 3 years"},
        "mandatory_requirements": [
            "£5m public liability insurance",
            "ISO 9001:2015",
        ],
        "scoring": {
            "summary": "60% quality / 40% price",
            "criteria": [
                {"criterion": "Method", "weight": "30%"},
                {"criterion": "Social value", "weight": "10%"},
            ],
        },
        "scope_summary": "Three-year hard FM contract across 12 sites.",
        "notable_conditions": ["10% performance bond", "PCG required"],
        "missing_or_unclear": ["Mobilisation period not stated"],
    }
)


@pytest.fixture()
def session() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    sess = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        sess.close()
        outer.rollback()
        connection.close()


def _make_tender(session: Session, ref: str) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="FTS",
        source_ref=ref,
        title="Hard FM",
        buyer_name="A Council",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(t)
    session.flush()
    return t


def _add_content(
    session: Session,
    tender: Tender,
    *,
    filename: str,
    text: str,
    doc_type: str = "docx",
    extraction_status: str = "ok",
) -> TenderDocumentContent:
    """Insert a document_file + corresponding content row directly. We skip
    the disk roundtrip — the generator reads from the content store, not from
    files."""
    f = TenderDocumentFile(
        tender_id=tender.id,
        url=f"https://e/{filename}",
        title=filename,
        format=doc_type,
        storage_key=None,
        sha256=f"sha-{filename}",
        download_status="ok",
        downloaded_at=datetime.now(UTC),
    )
    session.add(f)
    session.flush()
    c = TenderDocumentContent(
        document_file_id=f.id,
        tender_id=tender.id,
        sha256=f"sha-{filename}",
        extracted_text=text,
        char_count=len(text),
        extraction_status=extraction_status,
        extraction_detail=None,
        doc_type=doc_type,
        extractor_version=EXTRACTOR_VERSION,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(c)
    session.flush()
    return c


async def test_generate_brief_happy_path_populates_row(session):
    tender = _make_tender(session, "happy")
    _add_content(session, tender, filename="ITT.docx", text="The buyer requires…")
    fake = FakeLLMClient(VALID_BRIEF_JSON)

    outcome = await generate_brief(session, tender, llm=fake)

    assert outcome.status == "complete"
    assert outcome.payload is not None
    assert outcome.payload.recommendation == "bid"
    assert outcome.brief.status == "complete"
    assert outcome.brief.recommendation == "bid"
    assert outcome.brief.confidence == "medium"
    assert outcome.brief.headline.startswith("Strong scope-fit")
    assert outcome.brief.brief_json is not None
    assert outcome.brief.brief_json["key_risks"][0]["severity"] == "high"
    assert outcome.brief.input_tokens == 1000
    assert outcome.brief.output_tokens == 800
    # documents_considered records what went into the prompt.
    considered = outcome.brief.documents_considered
    assert considered and any(d["filename"] == "ITT.docx" for d in considered)
    assert all("included" in d for d in considered)


async def test_no_documents_returns_clean_state_and_no_llm_call(session):
    tender = _make_tender(session, "empty")
    fake = FakeLLMClient(VALID_BRIEF_JSON)

    outcome = await generate_brief(session, tender, llm=fake)

    assert outcome.status == "no_documents"
    assert outcome.payload is None
    assert outcome.brief.status == "no_documents"
    assert "fetch documents first" in (outcome.brief.error_detail or "").lower()
    # CRITICAL: zero LLM calls when there's nothing to analyse.
    assert fake.calls == []


async def test_malformed_json_retries_then_succeeds(session):
    tender = _make_tender(session, "retry-ok")
    _add_content(session, tender, filename="ITT.docx", text="x")

    fake = FakeLLMClient(["not json at all", VALID_BRIEF_JSON])
    outcome = await generate_brief(session, tender, llm=fake)
    assert outcome.status == "complete"
    assert len(fake.calls) == 2
    # The retry prompt is the stricter one.
    assert "ONLY the JSON object" in fake.calls[1]["user"]


async def test_malformed_json_twice_fails_cleanly(session):
    tender = _make_tender(session, "retry-fail")
    _add_content(session, tender, filename="ITT.docx", text="x")

    fake = FakeLLMClient(["garbage", "still garbage"])
    outcome = await generate_brief(session, tender, llm=fake)
    assert outcome.status == "failed"
    assert outcome.brief.status == "failed"
    assert "non-JSON" in (outcome.brief.error_detail or "")
    # And nothing parses as a brief.
    assert outcome.brief.recommendation is None
    assert outcome.brief.brief_json is None


async def test_brief_strips_markdown_fences(session):
    tender = _make_tender(session, "fenced")
    _add_content(session, tender, filename="ITT.docx", text="x")
    fenced = "```json\n" + VALID_BRIEF_JSON + "\n```"
    fake = FakeLLMClient(fenced)

    outcome = await generate_brief(session, tender, llm=fake)
    assert outcome.status == "complete"


def test_token_budget_prioritises_itt_and_truncates_spreadsheets():
    """The selector is the heart of token budgeting — test it directly."""
    # Build content rows of varying sizes/types. The xlsx is large enough to
    # need truncation; an "other" doc is large enough to get omitted under a
    # tight budget; the ITT must always make the cut.
    big_text = "row | row | row\n" * 5000  # > SPREADSHEET_TRUNCATE_CHARS
    other_text = "x" * 200_000

    contents = [
        _bare_content("price.xlsx", big_text, doc_type="xlsx"),
        _bare_content("Other supporting.pdf", other_text, doc_type="pdf"),
        _bare_content("ITT instructions.docx", "ITT body", doc_type="docx"),
        _bare_content("Quality questionnaire.docx", "Q body", doc_type="docx"),
        _bare_content("Specification.docx", "Spec body", doc_type="docx"),
    ]

    # Tight budget: enough for the priority docs + the truncated spreadsheet,
    # but not the unrelated 200KB "other" pdf.
    included, all_slots = _select_documents(contents, max_chars=30_000)
    included_names = [s.filename for s in included]

    # The priority order must put ITT first.
    assert included_names[0] == "ITT instructions.docx"
    # The spreadsheet survives — but only as truncated.
    xlsx = next(s for s in all_slots if s.filename == "price.xlsx")
    assert xlsx.included == "truncated"
    # The oversize "other" pdf was dropped to fit the budget.
    other = next(s for s in all_slots if s.filename == "Other supporting.pdf")
    assert other.included == "omitted"


def _bare_content(
    filename: str, text: str, *, doc_type: str
) -> TenderDocumentContent:
    """Build an unsaved TenderDocumentContent with a stub document_file so
    `_filename_for` resolves the title without hitting the DB."""
    f = TenderDocumentFile(
        tender_id=0,
        url=f"https://e/{filename}",
        title=filename,
        format=doc_type,
        storage_key=None,
        sha256=f"sha-{filename}",
        download_status="ok",
    )
    c = TenderDocumentContent(
        document_file_id=0,
        tender_id=0,
        sha256=f"sha-{filename}",
        extracted_text=text,
        char_count=len(text),
        extraction_status="ok",
        doc_type=doc_type,
        extractor_version=EXTRACTOR_VERSION,
    )
    # Wire the relationship in-memory so the generator's filename lookup
    # doesn't need a session.
    c.document_file = f
    return c


def test_brief_payload_normalises_no_bid_aliases():
    obj = json.loads(VALID_BRIEF_JSON)
    obj["recommendation"] = "no-bid"
    p = BriefPayload.model_validate_json(json.dumps(obj))
    assert p.recommendation == "no_bid"


def test_brief_payload_rejects_unknown_recommendation():
    from pydantic import ValidationError

    obj = json.loads(VALID_BRIEF_JSON)
    obj["recommendation"] = "maybe"
    with pytest.raises(ValidationError):
        BriefPayload.model_validate_json(json.dumps(obj))


def test_default_max_input_tokens_is_positive():
    # The constant is captured at module import — guard the contract here so
    # an accidental zero/negative override would break the test rather than
    # silently giving every brief a zero-char budget.
    assert bg_module.DEFAULT_MAX_INPUT_TOKENS > 0


def test_anthropic_client_raises_clear_error_without_key(monkeypatch):
    # Production client must surface a clear, actionable error rather than a
    # 500 when ANTHROPIC_API_KEY is unset.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "tender_agent.config.settings.anthropic_api_key", ""
    )
    client = AnthropicBriefClient(api_key=None)
    with pytest.raises(LLMConfigError) as exc:
        client._resolve_key()
    assert "ANTHROPIC_API_KEY" in str(exc.value)
