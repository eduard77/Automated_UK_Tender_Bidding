"""Fixture-based tests for the eTendersNI (NI) adapter.

NI is the odd one out — it parses an Atom XML feed rather than OCDS JSON, and
it post-filters entries by their `<updated>` element against the `since` cutoff
(the other four adapters trust the upstream's date filter). All HTTP mocked
via httpx.MockTransport.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tender_agent.adapters.etendersni import ETendersNIAdapter
from tender_agent.schemas import NormalisedTender

from .conftest import build_adapter, collect, load_text_fixture, static_text_handler

FIXTURE = "etendersni_feed.xml"


async def test_fetch_since_yields_normalised_tenders() -> None:
    """Pattern 1: 5 well-formed entries pass through (the malformed one without
    a <title> is skipped). With an ancient cutoff every dated entry survives."""
    atom = load_text_fixture(FIXTURE)
    adapter = build_adapter(ETendersNIAdapter, static_text_handler(atom))

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))

    assert len(tenders) == 5
    for t in tenders:
        assert isinstance(t, NormalisedTender)
        assert t.source_code == "NI"
        assert t.source_ref
        assert t.title
        # Adapter always pins NI status + country.
        assert t.status == "active"
        assert t.buyer_country == "Northern Ireland"


async def test_fetch_since_respects_cutoff() -> None:
    """Pattern 2: NI is the one adapter that post-filters by date. The fixture's
    "Archive: legacy IT migration (2026-02-01)" entry is BEFORE the 2026-04-01
    cutoff and must NOT appear. The other four well-formed + dated-after entries
    survive."""
    atom = load_text_fixture(FIXTURE)
    adapter = build_adapter(ETendersNIAdapter, static_text_handler(atom))

    cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    tenders = await collect(adapter, cutoff)

    titles = [t.title for t in tenders]
    assert "Archive: legacy IT migration services (pre-cutoff)" not in titles
    assert len(tenders) == 4
    for t in tenders:
        assert t.published_at is not None
        assert t.published_at >= cutoff


async def test_normalisation_of_known_fields() -> None:
    """Pattern 3: the first entry maps to its NormalisedTender exactly. NI's
    `_entry_to_tender` extracts source_ref via regex over the link URL."""
    atom = load_text_fixture(FIXTURE)
    adapter = build_adapter(ETendersNIAdapter, static_text_handler(atom))

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))
    t = next(t for t in tenders if t.source_ref == "NI-2026-50001")

    assert t.source_code == "NI"
    assert t.title == "Highways winter gritting — Belfast and surrounding areas"
    assert t.source_url == (
        "https://etendersni.gov.uk/epps/cft/viewContractNotice.do?notice/id/NI-2026-50001"
    )
    assert t.buyer_name == "Department for Infrastructure (NI)"
    assert t.description.startswith("Provision of winter gritting")
    assert t.notice_type == "tender"
    assert t.status == "active"
    assert t.buyer_country == "Northern Ireland"
    # Adapter sets published_at from <updated>; ensure timezone-aware.
    assert t.published_at is not None
    assert t.published_at.tzinfo is not None
    assert t.published_at == datetime(2026, 4, 28, 9, 15, 0, tzinfo=UTC)


async def test_handles_missing_optional_fields() -> None:
    """Pattern 4: an entry with no summary, no author. The adapter still yields
    a NormalisedTender; the optional fields are None (not the string "None")."""
    atom = load_text_fixture(FIXTURE)
    adapter = build_adapter(ETendersNIAdapter, static_text_handler(atom))

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))
    minimal = next(t for t in tenders if t.source_ref == "NI-2026-50005")

    assert minimal.title == "Minimal entry — title and link only"
    assert minimal.description is None
    assert minimal.buyer_name is None
    # Pinned defaults stay set even on a minimal entry.
    assert minimal.buyer_country == "Northern Ireland"
    assert minimal.status == "active"


async def test_handles_malformed_entry_gracefully() -> None:
    """Pattern 5: the fixture's last entry has no <title>; the adapter raises
    _SkipEntryError internally, catches it, and continues. The page still
    yields the other 5 well-formed entries."""
    atom = load_text_fixture(FIXTURE)
    adapter = build_adapter(ETendersNIAdapter, static_text_handler(atom))

    tenders = await collect(adapter, datetime(2025, 1, 1, tzinfo=UTC))

    assert len(tenders) == 5
    refs = [t.source_ref for t in tenders]
    assert "NI-2026-MALFORMED" not in refs
