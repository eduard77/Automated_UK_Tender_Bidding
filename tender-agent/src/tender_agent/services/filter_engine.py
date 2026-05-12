"""Filter engine. Decides which user filter profiles a tender matches."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import FilterProfile, Tender


def _text_blob(t: Tender) -> str:
    parts = [t.title or "", t.description or "", t.buyer_name or ""]
    return " ".join(parts).lower()


def _cpv_matches(profile: FilterProfile, tender_cpv: list[str] | None) -> bool:
    if not (profile.cpv_codes or profile.cpv_prefixes):
        return True
    if not tender_cpv:
        return False
    if profile.cpv_codes and any(c in profile.cpv_codes for c in tender_cpv):
        return True
    return bool(
        profile.cpv_prefixes
        and any(c.startswith(p) for c in tender_cpv for p in profile.cpv_prefixes)
    )


def _keyword_checks(profile: FilterProfile, blob: str) -> bool:
    if profile.keywords_none and any(k.lower() in blob for k in profile.keywords_none):
        return False
    if profile.keywords_all and not all(k.lower() in blob for k in profile.keywords_all):
        return False
    return not (profile.keywords_any and not any(k.lower() in blob for k in profile.keywords_any))


def _value_check(profile: FilterProfile, value: Decimal | None) -> bool:
    if profile.value_min is not None and (value is None or value < profile.value_min):
        return False
    return not (profile.value_max is not None and value is not None and value > profile.value_max)


def _list_match(profile_values, tender_value) -> bool:
    if not profile_values:
        return True
    if not tender_value:
        return False
    return any(v.lower() == tender_value.lower() for v in profile_values)


def _deadline_check(profile: FilterProfile, deadline: datetime | None) -> bool:
    if profile.min_days_to_deadline is None:
        return True
    if deadline is None:
        return True
    delta = (deadline - datetime.now(UTC)).total_seconds() / 86400
    return delta >= profile.min_days_to_deadline


def matches(profile: FilterProfile, tender: Tender) -> bool:
    """Return True iff tender satisfies all filter criteria on the profile."""
    if not _cpv_matches(profile, tender.cpv_codes):
        return False
    if not _keyword_checks(profile, _text_blob(tender)):
        return False
    if not _value_check(profile, tender.value_amount):
        return False
    if not _list_match(profile.buyer_names, tender.buyer_name):
        return False
    if not _list_match(profile.regions, tender.buyer_region):
        return False
    if not _list_match(profile.countries, tender.buyer_country):
        return False
    if not _list_match(profile.notice_types, tender.notice_type):
        return False
    return _deadline_check(profile, tender.deadline_at)


def matching_profiles(db: Session, tender: Tender) -> list[FilterProfile]:
    """Return all enabled profiles that match the given tender."""
    profiles = db.execute(
        select(FilterProfile).where(FilterProfile.enabled.is_(True))
    ).scalars().all()
    return [p for p in profiles if matches(p, tender)]
