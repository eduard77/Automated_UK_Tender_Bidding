"""Sector classifier tests (Phase 3a) — taxonomy validation, the per-call
classify path, persistence/idempotency, and the worker pass.

100% offline: the Anthropic client is a fake whose `messages.create` returns
canned JSON. No network, no Postgres (in-memory SQLite via the billing fixture).
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tender_agent.models import Tender
from tender_agent.services.classification.classifier import (
    classify_and_store,
    classify_text,
    parse_classification,
    process_pending_classification,
)
from tender_agent.services.classification.taxonomy import (
    CLASSIFIER_VERSION,
    canonical_sector,
    canonical_subcategory,
)
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)

_OTHER_JSON = (
    '{"primary_sector": "Other / Uncategorised", "secondary_sectors": [], '
    '"construction_subcategories": []}'
)


# --- fake Anthropic client -------------------------------------------------


class _FakeMessage:
    def __init__(self, text: str, in_tok: int = 12, out_tok: int = 6) -> None:
        self.content = [SimpleNamespace(type="text", text=text)]
        self.usage = SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok)


class _FakeMessages:
    def __init__(self, responses, default: str) -> None:
        self._responses = list(responses)
        self._default = default
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._responses.pop(0) if self._responses else self._default
        return _FakeMessage(text)


class _FakeClient:
    def __init__(self, responses=(), default: str = _OTHER_JSON) -> None:
        self.messages = _FakeMessages(responses, default)


# --- taxonomy validation ---------------------------------------------------


def test_canonical_sector_is_normalisation_tolerant() -> None:
    assert canonical_sector("Construction & Built Environment") == (
        "Construction & Built Environment"
    )
    # lower-case + an en-dash where the canonical has none still resolves.
    assert canonical_sector("it & digital") == "IT & Digital"
    assert canonical_sector("Made Up Sector") is None
    assert canonical_sector(None) is None


def test_canonical_subcategory_folds_dash_variants() -> None:
    # canonical uses an en-dash; a hyphen still matches, returns canonical.
    assert canonical_subcategory("New build - residential") == (
        "New build – residential"
    )
    assert canonical_subcategory("Mechanical & electrical (M&E)") == (
        "Mechanical & electrical (M&E)"
    )
    assert canonical_subcategory("not a subcategory") is None


def test_parse_only_stores_allowed_values() -> None:
    result = parse_classification(
        '{"primary_sector": "Construction & Built Environment",'
        ' "secondary_sectors": ["IT & Digital", "Banana Sector"],'
        ' "construction_subcategories": ["Refurbishment & renovation",'
        ' "Underwater basket weaving"]}'
    )
    assert result is not None
    assert result.primary_sector == "Construction & Built Environment"
    assert result.secondary_sectors == ["IT & Digital"]  # bogus dropped
    assert result.construction_subcategories == ["Refurbishment & renovation"]


def test_parse_invalid_primary_returns_none() -> None:
    assert parse_classification('{"primary_sector": "Nope"}') is None
    assert parse_classification("not json at all") is None
    assert parse_classification("[]") is None


def test_subcategory_only_when_construction_in_sectors() -> None:
    # Primary IT, no construction anywhere → subcategories dropped.
    r1 = parse_classification(
        '{"primary_sector": "IT & Digital", "secondary_sectors": [],'
        ' "construction_subcategories": ["Flooring"]}'
    )
    assert r1 is not None and r1.construction_subcategories == []

    # Construction as a SECONDARY sector → subcategories kept.
    r2 = parse_classification(
        '{"primary_sector": "IT & Digital",'
        ' "secondary_sectors": ["Construction & Built Environment"],'
        ' "construction_subcategories": ["Flooring"]}'
    )
    assert r2 is not None and r2.construction_subcategories == ["Flooring"]


# --- classify_text (per-call, with retry) ----------------------------------


def test_classify_school_refurb_construction_with_subcategories() -> None:
    client = _FakeClient(
        responses=[
            '{"primary_sector": "Construction & Built Environment",'
            ' "secondary_sectors": [],'
            ' "construction_subcategories": ["Refurbishment & renovation",'
            ' "Mechanical & electrical (M&E)"]}'
        ]
    )
    result = classify_text(
        "Refurbishment of primary school classrooms",
        "Strip-out and refit including M&E.",
        "Bristol City Council",
        client=client,
    )
    assert result.primary_sector == "Construction & Built Environment"
    assert result.construction_subcategories == [
        "Refurbishment & renovation",
        "Mechanical & electrical (M&E)",
    ]
    assert result.valid is True
    assert result.input_tokens == 12 and result.output_tokens == 6


def test_classify_it_health_primary_secondary() -> None:
    client = _FakeClient(
        responses=[
            '{"primary_sector": "IT & Digital",'
            ' "secondary_sectors": ["Health & Social Care"],'
            ' "construction_subcategories": []}'
        ]
    )
    result = classify_text("Patient records IT system for an NHS trust", None, client=client)
    assert result.primary_sector == "IT & Digital"
    assert result.secondary_sectors == ["Health & Social Care"]
    assert result.construction_subcategories == []


def test_classify_retries_once_then_succeeds() -> None:
    client = _FakeClient(
        responses=[
            "this is not json",  # attempt 1 invalid
            '{"primary_sector": "Facilities & Estates", "secondary_sectors": [],'
            ' "construction_subcategories": []}',  # attempt 2 valid
        ]
    )
    result = classify_text("Staff canteen catering contract", None, client=client)
    assert result.primary_sector == "Facilities & Estates"
    assert result.valid is True
    assert len(client.messages.calls) == 2  # retried once


def test_classify_invalid_twice_falls_back_to_other() -> None:
    client = _FakeClient(responses=["garbage", "{still bad"])
    result = classify_text("???", None, client=client)
    assert result.primary_sector == "Other / Uncategorised"
    assert result.valid is False
    assert len(client.messages.calls) == 2  # tried, retried, then gave up


# --- classify_and_store + worker (persistence) -----------------------------


def _add_tender(db, *, source_code="FTS", source_ref="r1", title="A tender",
                description=None) -> Tender:
    tender = Tender(
        source_code=source_code,
        source_ref=source_ref,
        title=title,
        description=description,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    db.add(tender)
    db.commit()
    db.refresh(tender)
    return tender


def test_classify_and_store_persists_and_is_idempotent() -> None:
    _engine, factory = make_engine_and_session()
    construction = (
        '{"primary_sector": "Construction & Built Environment",'
        ' "secondary_sectors": [],'
        ' "construction_subcategories": ["Refurbishment & renovation"]}'
    )
    with factory() as db:
        tender = _add_tender(
            db, source_ref="x1", title="School refurbishment", description="Refit"
        )
        result = classify_and_store(db, tender, client=_FakeClient([construction]))
        assert result is not None and result.primary_sector == (
            "Construction & Built Environment"
        )

    with factory() as db:
        stored = db.query(Tender).filter_by(source_ref="x1").one()
        assert stored.primary_sector == "Construction & Built Environment"
        assert stored.secondary_sectors == []
        assert stored.subcategories == {
            "Construction & Built Environment": ["Refurbishment & renovation"]
        }
        assert stored.construction_subcategories == ["Refurbishment & renovation"]
        assert stored.classifier_version == CLASSIFIER_VERSION
        assert stored.classified_at is not None

        # Already current → skipped, no second API call.
        idempotent_client = _FakeClient([construction])
        assert classify_and_store(db, stored, client=idempotent_client) is None
        assert idempotent_client.messages.calls == []


def test_process_pending_classification_classifies_unclassified_only() -> None:
    _engine, factory = make_engine_and_session()
    it_json = (
        '{"primary_sector": "IT & Digital", "secondary_sectors": [],'
        ' "construction_subcategories": []}'
    )
    with factory() as db:
        a = _add_tender(db, source_ref="a", title="IT system")
        _add_tender(db, source_ref="b", title="Cleaning contract")
        # Already on the current version — must be left untouched.
        done = _add_tender(db, source_ref="c", title="Done")
        done.primary_sector = "Health & Social Care"
        done.classifier_version = CLASSIFIER_VERSION
        # A duplicate — excluded from the pending set.
        dup = _add_tender(db, source_ref="d", title="Dup")
        dup.duplicate_of_id = a.id
        db.commit()

        processed = process_pending_classification(
            db, limit=10, client=_FakeClient(default=it_json)
        )
        assert processed == 2  # a + b only

    with factory() as db:
        assert db.query(Tender).filter_by(source_ref="a").one().primary_sector == (
            "IT & Digital"
        )
        assert db.query(Tender).filter_by(source_ref="b").one().primary_sector == (
            "IT & Digital"
        )
        # Pre-classified row kept its original sector (not reclassified).
        assert db.query(Tender).filter_by(source_ref="c").one().primary_sector == (
            "Health & Social Care"
        )
        # Duplicate never classified.
        assert db.query(Tender).filter_by(source_ref="d").one().primary_sector is None


def test_process_pending_classification_empty_returns_zero() -> None:
    _engine, factory = make_engine_and_session()
    with factory() as db:
        assert process_pending_classification(db, limit=5, client=_FakeClient()) == 0
