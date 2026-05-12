"""OCDS release -> NormalisedTender conversion.

Both Find a Tender and Contracts Finder publish OCDS release packages, so a
single conversion handles both. Sources that aren't OCDS-based (Sell2Wales RSS
etc., added in Pass 2) will have their own normalisers.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from dateutil import parser as dtparser

from tender_agent.schemas import NormalisedTender, TenderDocument


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return dtparser.isoparse(str(value))
    except (ValueError, TypeError):
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _coerce_status(tender_obj: dict) -> str | None:
    """Map OCDS tender.status to our status vocab."""
    status = (tender_obj.get("status") or "").lower()
    return {
        "active": "active",
        "planned": "planned",
        "planning": "planned",
        "complete": "closed",
        "completed": "closed",
        "cancelled": "cancelled",
        "withdrawn": "cancelled",
        "unsuccessful": "closed",
    }.get(status, status or None)


def _coerce_notice_type(release: dict) -> str | None:
    """Extract notice type from OCDS tag(s)."""
    tags = release.get("tag") or []
    if not tags:
        return None
    priority = ["tender", "tenderUpdate", "award", "contract", "planning"]
    for p in priority:
        if p in tags:
            return p
    return tags[0]


def ocds_release_to_tender(
    release: dict,
    *,
    source_code: str,
    source_url_template: str | None = None,
) -> NormalisedTender:
    """Convert one OCDS release to a NormalisedTender."""
    ocid = release.get("ocid") or release.get("id") or ""
    if not ocid:
        raise ValueError("OCDS release missing ocid")

    tender_obj: dict = release.get("tender") or {}
    buyer_obj: dict = release.get("buyer") or {}
    parties = release.get("parties") or []

    # Buyer details — may be in `buyer` reference and resolved via `parties`
    buyer_name = buyer_obj.get("name")
    buyer_id = buyer_obj.get("id")
    buyer_country = None
    buyer_region = None
    if buyer_id and parties:
        for party in parties:
            if party.get("id") == buyer_id:
                addr = party.get("address") or {}
                buyer_country = addr.get("countryName") or addr.get("country")
                buyer_region = addr.get("region") or addr.get("locality")
                buyer_name = buyer_name or party.get("name")
                break

    # Value
    value_obj = tender_obj.get("value") or {}
    value_amount = _parse_decimal(value_obj.get("amount"))
    value_currency = value_obj.get("currency")

    min_value_obj = tender_obj.get("minValue") or {}
    value_min = _parse_decimal(min_value_obj.get("amount"))

    # CPV codes — OCDS uses items[].classification (mainly) and additionalClassifications
    cpv_codes: list[str] = []
    main_class = tender_obj.get("classification") or {}
    if main_class.get("scheme", "").upper().startswith("CPV") and main_class.get("id"):
        cpv_codes.append(str(main_class["id"]))
    for item in tender_obj.get("items") or []:
        ic = item.get("classification") or {}
        if ic.get("scheme", "").upper().startswith("CPV") and ic.get("id"):
            cpv_codes.append(str(ic["id"]))
        for ac in item.get("additionalClassifications") or []:
            if ac.get("scheme", "").upper().startswith("CPV") and ac.get("id"):
                cpv_codes.append(str(ac["id"]))
    cpv_codes = sorted(set(cpv_codes))

    # Dates
    tender_period = tender_obj.get("tenderPeriod") or {}
    deadline_at = _parse_datetime(tender_period.get("endDate"))
    published_at = _parse_datetime(release.get("date"))

    contract_period = tender_obj.get("contractPeriod") or {}
    contract_start_dt = _parse_datetime(contract_period.get("startDate"))
    contract_end_dt = _parse_datetime(contract_period.get("endDate"))

    # Documents
    docs: list[TenderDocument] = []
    for d in tender_obj.get("documents") or []:
        url = d.get("url")
        if url:
            docs.append(
                TenderDocument(
                    title=d.get("title"),
                    url=url,
                    format=d.get("format"),
                )
            )

    source_url = (
        source_url_template.format(ocid=ocid) if source_url_template else None
    )

    return NormalisedTender(
        source_code=source_code,
        source_ref=ocid,
        source_url=source_url,
        procurement_ref=tender_obj.get("id"),
        title=tender_obj.get("title") or release.get("title") or "(untitled)",
        description=tender_obj.get("description"),
        notice_type=_coerce_notice_type(release),
        status=_coerce_status(tender_obj),
        buyer_name=buyer_name,
        buyer_id=buyer_id,
        buyer_country=buyer_country,
        buyer_region=buyer_region,
        cpv_codes=cpv_codes,
        keywords=[],
        value_amount=value_amount,
        value_currency=value_currency,
        value_min=value_min,
        published_at=published_at,
        deadline_at=deadline_at,
        contract_start=contract_start_dt.date() if contract_start_dt else None,
        contract_end=contract_end_dt.date() if contract_end_dt else None,
        documents=docs,
        raw=release,
    )
