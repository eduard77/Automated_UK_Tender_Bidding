"""Tests for the CF/FTS onward-route classifier.

Pure string classification — no DB, no network. Each test feeds a text blob
(the kind ``scripts/survey_cf_onward_routes.py`` assembles from a tender) to
``classify_text`` and asserts the bucket / portal / detail.
"""
from __future__ import annotations

from tender_agent.diagnostics.onward_routes import (
    BUCKET_DIRECT,
    BUCKET_EMAIL,
    BUCKET_GENERIC,
    BUCKET_NONE,
    classify_text,
    extract_emails,
    extract_urls,
    match_portal,
)


def test_delta_access_code_url_is_direct() -> None:
    blob = "Respond at https://www.delta-esourcing.com/respond/12AB34CD56?accessCode=12AB34CD56"
    result = classify_text(blob)
    assert result.bucket == BUCKET_DIRECT
    assert result.portal == "delta"
    assert "delta-esourcing.com" in result.detail


def test_procontract_advert_url_is_direct() -> None:
    blob = (
        "Apply via the portal: "
        "https://procontract.due-north.com/Advert?advertId=8f2c1a90-0000-4abc-9999-aaaaaaaaaaaa"
    )
    result = classify_text(blob)
    assert result.bucket == BUCKET_DIRECT
    assert result.portal == "procontract"


def test_intend_project_url_is_direct() -> None:
    blob = "https://council.in-tend.co.uk/aspx/Tenders/Manage?ProjectId=4471"
    result = classify_text(blob)
    assert result.bucket == BUCKET_DIRECT
    assert result.portal == "intend"


def test_bare_portal_home_is_generic() -> None:
    blob = "Register your interest on the e-sourcing portal: https://www.delta-esourcing.com/"
    result = classify_text(blob)
    assert result.bucket == BUCKET_GENERIC
    assert result.portal == "delta"
    assert result.detail == "https://www.delta-esourcing.com/"


def test_buyer_website_only_is_none() -> None:
    """A non-portal buyer website with no email is not an actionable route."""
    blob = "See https://www.wiltshire.gov.uk/business-tenders for more information."
    result = classify_text(blob)
    assert result.bucket == BUCKET_NONE
    assert result.portal is None
    assert result.detail is None


def test_mailto_only_is_email_only() -> None:
    blob = "To express interest, email mailto:procurement@example-council.gov.uk by the deadline."
    result = classify_text(blob)
    assert result.bucket == BUCKET_EMAIL
    assert result.detail == "procurement@example-council.gov.uk"


def test_contact_email_in_text_is_email_only() -> None:
    blob = 'Contact: {"contactPoint": {"name": "Jane Doe", "email": "jane.doe@nhs.net"}}'
    result = classify_text(blob)
    assert result.bucket == BUCKET_EMAIL
    assert result.detail == "jane.doe@nhs.net"


def test_no_link_no_email_is_none() -> None:
    result = classify_text("Catering supplies — small-value notice. No further details provided.")
    assert result.bucket == BUCKET_NONE
    assert result.portal is None
    assert result.detail is None


def test_empty_input_is_none() -> None:
    assert classify_text("").bucket == BUCKET_NONE
    assert classify_text(None).bucket == BUCKET_NONE


def test_direct_link_wins_over_email() -> None:
    """A specific portal link beats a contact email in the same notice."""
    blob = (
        "Questions to buyer@council.gov.uk. Respond on Delta: "
        "https://www.delta-esourcing.com/tenders/UK-UK-Council-Services?noticeId=99887"
    )
    result = classify_text(blob)
    assert result.bucket == BUCKET_DIRECT
    assert result.portal == "delta"


def test_source_url_passed_as_extra_url_is_considered() -> None:
    """source_url may carry the route even when the blob text doesn't."""
    result = classify_text(
        "No links in the body.",
        extra_urls=("https://uk.eu-supply.com/ctm/Supplier/PublicTenders/ViewNotice/12345",),
    )
    assert result.bucket == BUCKET_DIRECT
    assert result.portal == "eu_supply"


def test_aggregator_self_link_is_not_a_portal() -> None:
    """A Contracts Finder document URL is the notice itself, not an onward portal."""
    blob = (
        "Specification: "
        "https://www.contractsfinder.service.gov.uk/Notice/cf-2026-200001/spec.pdf"
    )
    result = classify_text(blob)
    assert result.bucket == BUCKET_NONE


def test_extract_urls_strips_trailing_punctuation_and_dedupes() -> None:
    urls = extract_urls("See https://a.example/x. Again https://a.example/x, and https://b.example/y).")
    assert urls == ["https://a.example/x", "https://b.example/y"]


def test_extract_emails_ignores_at_signs_inside_urls() -> None:
    emails = extract_emails("Login at https://portal.example/path?u=name@host and write to a@b.org")
    assert emails == ["a@b.org"]


def test_match_portal_returns_none_for_unknown_domain() -> None:
    assert match_portal("https://www.some-random-buyer.co.uk/tenders") is None
    assert match_portal("https://www.delta-esourcing.com/").name == "delta"
