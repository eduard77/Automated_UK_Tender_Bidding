"""Canonical region assignment (Phase 4 chunk 8).

The deterministic resolver tests are pure unit tests — no DB, no network. The
``normalise_all_tenders`` and ingestion-hook tests run against the real Postgres
session (like test_tender_search) and scope themselves to a controlled set.

The LLM is always mocked: a tiny fake ``complete`` coroutine that records calls,
so no real API is ever hit and the AI-cache behaviour is asserted by call count.
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import Tender
from tender_agent.schemas import NormalisedTender
from tender_agent.services.ingestion import _upsert_tender
from tender_agent.services.regions import (
    RegionLookupCache,
    normalise_all_tenders,
    resolve_region,
    resolve_region_ai,
)

_counter = itertools.count(1)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class FakeLLM:
    """Records each call; returns a fixed region string. Mockable stand-in for
    the brief LLM client — never touches the network."""

    model = "fake"

    def __init__(self, region: str) -> None:
        self._region = region
        self.calls: list[str] = []

    async def complete(self, *, system: str, user: str, max_tokens: int):
        self.calls.append(user)
        return SimpleNamespace(
            text=self._region, input_tokens=10, output_tokens=2, model=self.model
        )


def _addr_raw(**addr) -> dict:
    """A minimal OCDS release carrying one tender-item delivery address."""
    return {"tender": {"items": [{"deliveryAddresses": [addr]}]}}


# ---------------------------------------------------------------------------
# Priority a — OCDS region (exact / NUTS), no postcode/AI consulted
# ---------------------------------------------------------------------------


def test_ocds_region_exact_wins_over_postcode() -> None:
    # region "London" + an M-area postcode that would map to North West: the
    # exact region must win, and the method is ocds_region (postcode untouched).
    res = resolve_region(_addr_raw(region="London", postalCode="M1 3BN"), None, None)
    assert res.region == "London"
    assert res.method == "ocds_region"


def test_ocds_region_case_insensitive() -> None:
    res = resolve_region(_addr_raw(region="north west"), None, None)
    assert res.region == "North West"
    assert res.method == "ocds_region"


def test_british_oversea_territories_spelling_fixed() -> None:
    res = resolve_region(_addr_raw(region="British Oversea Territories"), None, None)
    assert res.region == "British Overseas Territories"
    assert res.method == "ocds_region"


def test_nuts_code_resolves_to_region() -> None:
    res = resolve_region(_addr_raw(region="UKM82"), None, None)
    assert res.region == "Scotland"
    assert res.method == "ocds_region"


# ---------------------------------------------------------------------------
# Priority b — UK postcode area (deterministic, no AI)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("postcode", "expected"),
    [
        ("M1 3BN", "North West"),
        ("S60 1AE", "Yorkshire and the Humber"),
        ("SN2 1FL", "South West"),
        ("EH1 1AA", "Scotland"),
        ("CF10 1AA", "Wales"),
        ("BT1 1AA", "Northern Ireland"),
    ],
)
def test_postcode_only_is_deterministic(postcode: str, expected: str) -> None:
    res = resolve_region(_addr_raw(postalCode=postcode), None, None)
    assert res.region == expected
    assert res.method == "postcode"


def test_postcode_beats_country() -> None:
    res = resolve_region(_addr_raw(postalCode="M1 3BN", countryName="Vietnam"), None, None)
    assert res.region == "North West"
    assert res.method == "postcode"


# ---------------------------------------------------------------------------
# Priority c — country name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("Vietnam", "Rest of the World"),
        ("United Kingdom", "United Kingdom"),
        ("France", "Europe"),
        ("Jersey", "Channel Islands"),
        ("Isle of Man", "Isle of Man"),
    ],
)
def test_country_only(country: str, expected: str) -> None:
    res = resolve_region(_addr_raw(countryName=country), None, None)
    assert res.region == expected
    assert res.method == "country"


# ---------------------------------------------------------------------------
# Priority e — nothing usable
# ---------------------------------------------------------------------------


def test_nothing_usable_is_unspecified() -> None:
    res = resolve_region({}, None, None)
    assert res.region == "Unspecified"
    assert res.method == "unspecified"
    assert res.ai_candidate is None


# ---------------------------------------------------------------------------
# Priority d — AI fallback (mocked, cached, called once per distinct key)
# ---------------------------------------------------------------------------


def test_ai_not_called_when_deterministic() -> None:
    fake = FakeLLM("London")
    cache = RegionLookupCache(None)
    res = asyncio.run(
        resolve_region_ai(_addr_raw(region="North West"), None, None, cache=cache, llm=fake)
    )
    assert res.region == "North West"
    assert res.method == "ocds_region"
    assert fake.calls == []  # deterministic — LLM never consulted


def test_unresolved_uses_ai_and_caches_per_key() -> None:
    # A messy free-text region string none of the deterministic paths resolve.
    fake = FakeLLM("North West")
    cache = RegionLookupCache(None)
    res = asyncio.run(
        resolve_region_ai({}, "Greater Manchester Combined Authority", None, cache=cache, llm=fake)
    )
    assert res.region == "North West"
    assert res.method == "ai"
    assert len(fake.calls) == 1

    # A second tender with the SAME unresolved key reuses the cache — no 2nd call.
    res2 = asyncio.run(
        resolve_region_ai({}, "Greater Manchester Combined Authority", None, cache=cache, llm=fake)
    )
    assert res2.region == "North West"
    assert len(fake.calls) == 1


def test_unresolved_without_llm_stays_unspecified() -> None:
    cache = RegionLookupCache(None)
    res = asyncio.run(
        resolve_region_ai({}, "Somewhere Odd", None, cache=cache, llm=None)
    )
    assert res.region == "Unspecified"
    assert res.method == "unspecified"


# ---------------------------------------------------------------------------
# DB-backed: normalise_all_tenders + ingestion hook
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


def _t(session: Session, **kw) -> Tender:
    now = datetime.now(UTC)
    n = next(_counter)
    defaults = {
        "source_code": "REGION_TEST",
        "source_ref": f"REGION_TEST-{n}",
        "title": f"Region tender {n}",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    defaults.update(kw)
    t = Tender(**defaults)
    session.add(t)
    session.flush()
    return t


def test_normalise_all_tenders_mixed_inputs(session: Session) -> None:
    # Park every pre-existing NULL-region row so the backfill only processes the
    # controlled set created below (the column is new, so the whole table is NULL).
    session.execute(
        update(Tender).where(Tender.region.is_(None)).values(region="Unspecified")
    )
    session.flush()

    t_region = _t(session, raw=_addr_raw(region="North West"))
    t_postcode = _t(session, raw=_addr_raw(postalCode="S60 1AE"))
    t_country = _t(session, raw=_addr_raw(countryName="Vietnam"))
    t_ai = _t(session, buyer_region="Greater Manchester Combined Authority")
    t_none = _t(session)

    fake = FakeLLM("London")
    counts = normalise_all_tenders(session, llm=fake)

    session.refresh(t_region)
    session.refresh(t_postcode)
    session.refresh(t_country)
    session.refresh(t_ai)
    session.refresh(t_none)

    assert t_region.region == "North West"
    assert t_postcode.region == "Yorkshire and the Humber"
    assert t_country.region == "Rest of the World"
    assert t_ai.region == "London"  # via mocked AI
    assert t_none.region == "Unspecified"

    assert counts["ocds_region"] == 1
    assert counts["postcode"] == 1
    assert counts["country"] == 1
    assert counts["ai"] == 1
    assert counts["unspecified"] == 1
    assert len(fake.calls) == 1


def test_normalise_all_tenders_is_rerunnable(session: Session) -> None:
    session.execute(
        update(Tender).where(Tender.region.is_(None)).values(region="Unspecified")
    )
    session.flush()
    _t(session, raw=_addr_raw(region="London"))

    first = normalise_all_tenders(session)
    assert sum(first.values()) == 1
    # Second run touches nothing (no NULL-region rows remain in our set).
    second = normalise_all_tenders(session)
    assert sum(second.values()) == 0


def test_ingestion_hook_sets_region_on_upsert(session: Session) -> None:
    nt = NormalisedTender(
        source_code="REGION_UPSERT",
        source_ref=f"upsert-{next(_counter)}",
        title="Upsert region tender",
        buyer_region="Manchester",
        buyer_country="United Kingdom",
        raw=_addr_raw(region="North West"),
    )
    tender, action = _upsert_tender(session, nt)
    assert action == "new"
    assert tender.region == "North West"
