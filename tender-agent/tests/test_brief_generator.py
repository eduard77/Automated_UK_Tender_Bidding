"""Brief generator with a FAKE LLM (chunk 5 Part B).

No real Anthropic calls. The fake client returns canned JSON; we assert the
generator validates it, persists tender_briefs rows, populates
documents_considered, and handles malformed-JSON / missing-key / empty-docs
paths.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import Tender, TenderBrief, TenderDocumentFile
from tender_agent.services.brief.brief_generator import (
    _select_documents,
    generate_brief,
    latest_brief,
)
from tender_agent.services.brief.content_store import (
    ensure_content_extracted,
)
from tender_agent.services.brief.llm_client import (
    BriefLLMClient,
    LLMConfigurationError,
    LLMResponse,
)

# --- Fixtures -----------------------------------------------------------------


@pytest.fixture()
def db() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


# --- Fake LLM client ----------------------------------------------------------


@dataclass
class _FakeLLM:
    """Mockable BriefLLMClient. By default returns canned valid JSON; pass
    responses=[...] to drive a sequence (used for the malformed-then-retry
    scenario)."""

    model: str = "fake-claude"
    responses: list[str] | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        if self.responses is None:
            self.responses = [_canned_valid_json()]
        self._idx = 0

    async def complete(
        self, *, system: str, user: str, max_tokens: int
    ) -> LLMResponse:
        self.calls.append((system, user, max_tokens))
        text = self.responses[min(self._idx, len(self.responses) - 1)]
        self._idx += 1
        return LLMResponse(
            text=text,
            input_tokens=1234,
            output_tokens=567,
            model=self.model,
        )


def _canned_valid_json() -> str:
    return json.dumps(
        {
            "recommendation": "bid",
            "confidence": "medium",
            "headline": "Mid-sized refurb — competitive but doable",
            "rationale": (
                "The scope aligns with our framework experience and the "
                "deadline gives 28 days. Pricing schedule is standard."
            ),
            "key_risks": [
                {
                    "risk": "Short programme",
                    "severity": "high",
                    "detail": "12-week build vs typical 16.",
                },
                {
                    "risk": "Bond requirement",
                    "severity": "medium",
                    "detail": "10% performance bond.",
                },
            ],
            "deadline": {"date": "2026-06-30", "note": "12:00 noon"},
            "contract_value": {"amount": "£1.2m", "note": "est."},
            "mandatory_requirements": [
                "£5m PL insurance",
                "ISO 9001",
            ],
            "scoring": {
                "summary": "60% price, 40% quality",
                "criteria": [
                    {"criterion": "Price", "weight": "60%"},
                    {"criterion": "Quality", "weight": "40%"},
                ],
            },
            "scope_summary": "Refurbish two-storey office block.",
            "notable_conditions": ["Retention 5%"],
            "missing_or_unclear": ["Asbestos survey not attached"],
        }
    )


# --- Test helpers -------------------------------------------------------------


def _seed_pack(db: Session, tmp_path, ref: str, docs: list[tuple[str, bytes]]) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="TEST",
        source_ref=ref,
        title=f"Tender {ref}",
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(t)
    db.flush()
    for name, body in docs:
        path = tmp_path / f"{ref}-{name}"
        path.write_bytes(body)
        f = TenderDocumentFile(
            tender_id=t.id,
            url=f"https://example.com/{name}",
            title=name,
            format=name.rsplit(".", 1)[-1],
            storage_key=str(path),
            storage_backend="local",
            bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            download_status="ok",
            downloaded_at=now,
            created_at=now,
        )
        db.add(f)
    db.flush()
    ensure_content_extracted(db, t)
    return t


# --- Tests --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_brief_validates_and_persists(db: Session, tmp_path) -> None:
    t = _seed_pack(
        db,
        tmp_path,
        "br-1",
        [
            ("ITT.txt", b"This is the Invitation to Tender for refurb."),
            ("quality_questionnaire.txt", b"Method statement required."),
        ],
    )
    llm = _FakeLLM()
    brief = await generate_brief(db, t.id, llm=llm)
    assert brief.status == "complete"
    assert brief.recommendation == "bid"
    assert brief.confidence == "medium"
    assert brief.headline
    assert brief.brief_json is not None
    assert brief.brief_json["recommendation"] == "bid"
    assert len(brief.brief_json["key_risks"]) == 2
    # documents_considered records every file with an inclusion status.
    assert brief.documents_considered is not None
    names = {d["filename"] for d in brief.documents_considered}
    assert {"ITT.txt", "quality_questionnaire.txt"}.issubset(names)
    for d in brief.documents_considered:
        assert d["included"] in {"full", "truncated", "omitted"}


@pytest.mark.asyncio
async def test_generate_brief_no_documents_fails_cleanly(db: Session) -> None:
    now = datetime.now(UTC)
    t = Tender(
        source_code="TEST",
        source_ref="br-empty",
        title="No docs",
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(t)
    db.flush()
    brief = await generate_brief(db, t.id, llm=_FakeLLM())
    assert brief.status == "failed"
    assert brief.error_detail
    assert "no documents" in (brief.error_detail or "").lower()


@pytest.mark.asyncio
async def test_malformed_json_retries_then_succeeds(db: Session, tmp_path) -> None:
    t = _seed_pack(db, tmp_path, "br-retry", [("ITT.txt", b"x")])
    llm = _FakeLLM(responses=["not json at all", _canned_valid_json()])
    brief = await generate_brief(db, t.id, llm=llm)
    assert brief.status == "complete"
    assert brief.recommendation == "bid"
    assert len(llm.calls) == 2  # first malformed, second succeeded


@pytest.mark.asyncio
async def test_malformed_json_twice_fails_cleanly(db: Session, tmp_path) -> None:
    t = _seed_pack(db, tmp_path, "br-fail", [("ITT.txt", b"x")])
    llm = _FakeLLM(responses=["garbage", "still garbage"])
    brief = await generate_brief(db, t.id, llm=llm)
    assert brief.status == "failed"
    assert brief.error_detail
    assert "invalid json" in brief.error_detail.lower()
    # No bid_json must be stored when validation fails.
    assert brief.brief_json is None


@pytest.mark.asyncio
async def test_missing_api_key_emits_clear_error(db: Session, tmp_path) -> None:
    t = _seed_pack(db, tmp_path, "br-nokey", [("ITT.txt", b"x")])

    class _RaisingLLM:
        model = "claude-test"

        async def complete(self, **kw):  # type: ignore[no-untyped-def]
            raise LLMConfigurationError(
                "ANTHROPIC_API_KEY not set — add it to the backend .env to "
                "generate briefs"
            )

    brief = await generate_brief(db, t.id, llm=_RaisingLLM())
    assert brief.status == "failed"
    assert "ANTHROPIC_API_KEY" in (brief.error_detail or "")


@pytest.mark.asyncio
async def test_invalid_recommendation_value_is_rejected_and_retried(
    db: Session, tmp_path
) -> None:
    """If the LLM returns a recommendation outside the allowed set, schema
    validation fails — generator retries once."""
    t = _seed_pack(db, tmp_path, "br-bad-rec", [("ITT.txt", b"x")])
    bad = json.dumps(
        {
            "recommendation": "maybe",  # invalid
            "confidence": "medium",
            "headline": "h",
            "rationale": "r",
            "key_risks": [],
        }
    )
    llm = _FakeLLM(responses=[bad, _canned_valid_json()])
    brief = await generate_brief(db, t.id, llm=llm)
    assert brief.status == "complete"
    assert len(llm.calls) == 2


def test_token_budgeting_prioritises_itt_truncates_xlsx(db: Session) -> None:
    """Direct unit test of the budgeting function: ITT/spec docs are kept
    in full at high priority; an oversized xlsx is truncated; lowest-priority
    overflow is omitted."""
    now = datetime.now(UTC)

    class _Doc:
        def __init__(self, file_id: int, title: str, fmt: str):
            self.id = file_id
            self.title = title
            self.url = f"https://example/{title}"
            self.format = fmt
            self.created_at = now

    class _Content:
        def __init__(self, text: str, status: str = "ok", doc_type: str | None = None):
            self.extracted_text = text
            self.extraction_status = status
            self.char_count = len(text) if text else 0
            self.sha256 = "sha-" + str(id(text))
            self.doc_type = doc_type

    pairs = [
        (
            _Doc(1, "Appendix A drawings.pdf", "pdf"),
            _Content("x" * 200_000, doc_type="pdf"),
        ),
        (
            _Doc(2, "ITT Instructions.docx", "docx"),
            _Content("ITT body " * 1000, doc_type="docx"),
        ),
        (
            _Doc(3, "Pricing Schedule.xlsx", "xlsx"),
            _Content("0123456789" * 50_000, doc_type="xlsx"),
        ),
        (
            _Doc(4, "Quality Questionnaire.docx", "docx"),
            _Content("Method statement " * 500, doc_type="docx"),
        ),
    ]

    # Tight budget — forces truncation/omission to kick in.
    out = _select_documents(pairs, budget_tokens=20_000)
    by_name = {p.filename: p for p in out}
    # ITT is the highest-priority doc; it must be fully included.
    assert by_name["ITT Instructions.docx"].included == "full"
    # Quality questionnaire is also high priority.
    assert by_name["Quality Questionnaire.docx"].included in {"full", "truncated"}
    # Drawings (low priority) most likely omitted under tight budget.
    assert by_name["Appendix A drawings.pdf"].included in {"omitted", "truncated"}
    # Pricing xlsx: large file → truncated (or omitted if no room).
    assert by_name["Pricing Schedule.xlsx"].included in {"truncated", "omitted"}


@pytest.mark.asyncio
async def test_generate_brief_records_token_usage(db: Session, tmp_path) -> None:
    t = _seed_pack(db, tmp_path, "br-tok", [("ITT.txt", b"x")])
    brief = await generate_brief(db, t.id, llm=_FakeLLM())
    assert brief.input_tokens == 1234
    assert brief.output_tokens == 567
    assert brief.model == "fake-claude"


@pytest.mark.asyncio
async def test_regenerate_adds_new_row_keeps_history(db: Session, tmp_path) -> None:
    t = _seed_pack(db, tmp_path, "br-hist", [("ITT.txt", b"x")])
    b1 = await generate_brief(db, t.id, llm=_FakeLLM())
    b2 = await generate_brief(db, t.id, llm=_FakeLLM())
    assert b1.id != b2.id
    rows = db.query(TenderBrief).filter(TenderBrief.tender_id == t.id).all()
    assert len(rows) >= 2
    latest = latest_brief(db, t.id)
    assert latest is not None
    assert latest.id == b2.id


@pytest.mark.asyncio
async def test_llm_call_failure_is_recorded_not_raised(db: Session, tmp_path) -> None:
    t = _seed_pack(db, tmp_path, "br-llm-fail", [("ITT.txt", b"x")])

    class _BoomLLM:
        model = "fake-claude"

        async def complete(self, **kw):  # type: ignore[no-untyped-def]
            raise RuntimeError("Anthropic timeout")

    brief = await generate_brief(db, t.id, llm=_BoomLLM())
    assert brief.status == "failed"
    assert "Anthropic" in (brief.error_detail or "")


def test_brief_llm_client_protocol_compatibility() -> None:
    """A simple structural check that the FakeLLM satisfies BriefLLMClient."""
    fake: BriefLLMClient = _FakeLLM()  # type: ignore[assignment]
    assert fake.model == "fake-claude"
