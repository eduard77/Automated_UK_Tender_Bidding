from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tender_agent.models import FilterProfile, Tender
from tender_agent.services.filter_engine import matches


def _tender(**overrides) -> Tender:
    base = dict(
        source_code="FTS",
        source_ref="ref-1",
        title="Cleaning services for schools",
        description="Daily cleaning across primary schools",
        buyer_name="Bristol City Council",
        buyer_country="United Kingdom",
        buyer_region="South West",
        cpv_codes=["90910000"],
        value_amount=Decimal("150000"),
        value_currency="GBP",
        notice_type="tender",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    base.update(overrides)
    return Tender(**base)


def test_cpv_prefix_match():
    p = FilterProfile(name="cleaning", cpv_prefixes=["909"])
    assert matches(p, _tender()) is True


def test_cpv_prefix_no_match():
    p = FilterProfile(name="it", cpv_prefixes=["72"])
    assert matches(p, _tender()) is False


def test_keywords_all_required():
    p = FilterProfile(name="x", keywords_all=["cleaning", "schools"])
    assert matches(p, _tender()) is True
    p2 = FilterProfile(name="x", keywords_all=["cleaning", "hospitals"])
    assert matches(p2, _tender()) is False


def test_keywords_none_excludes():
    p = FilterProfile(name="x", keywords_any=["cleaning"], keywords_none=["schools"])
    assert matches(p, _tender()) is False


def test_value_min_max():
    p = FilterProfile(name="x", value_min=Decimal("100000"), value_max=Decimal("200000"))
    assert matches(p, _tender()) is True
    p2 = FilterProfile(name="x", value_min=Decimal("500000"))
    assert matches(p2, _tender()) is False


def test_min_days_to_deadline():
    p = FilterProfile(name="x", min_days_to_deadline=14)
    assert matches(p, _tender()) is True
    p2 = FilterProfile(name="x", min_days_to_deadline=60)
    assert matches(p2, _tender()) is False


def test_region_match():
    p = FilterProfile(name="x", regions=["South West"])
    assert matches(p, _tender()) is True
    p2 = FilterProfile(name="x", regions=["Scotland"])
    assert matches(p2, _tender()) is False
