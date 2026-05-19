"""Unit tests for URL extraction primitives.

These tests are pure: no DB, no network. They cover the regex, normalisation,
and the gov.uk-apex special case.
"""
from __future__ import annotations

from tender_agent.services.portal_discovery import (
    is_gov_uk_apex,
    normalise_url,
)

# --- normalise_url ------------------------------------------------------


def test_normalise_lowercases_scheme_and_host():
    out = normalise_url("HTTPS://Procontract.Due-North.com/Tenders")
    assert out is not None
    norm, domain = out
    assert norm == "https://procontract.due-north.com/Tenders"
    assert domain == "procontract.due-north.com"


def test_normalise_strips_fragment_and_irrelevant_query():
    out = normalise_url(
        "https://portal.example.com/tender?utm_source=twitter#section-3"
    )
    assert out is not None
    norm, _ = out
    assert norm == "https://portal.example.com/tender"


def test_normalise_preserves_id_and_ref_query_keys():
    out = normalise_url(
        "https://portal.example.com/notice?utm=foo&id=12345&ref=abc"
    )
    assert out is not None
    norm, _ = out
    assert "id=12345" in norm
    assert "ref=abc" in norm
    assert "utm=foo" not in norm


def test_normalise_strips_www_prefix():
    out = normalise_url("https://www.in-tendhost.co.uk/buyer")
    assert out is not None
    _, domain = out
    assert domain == "in-tendhost.co.uk"


def test_normalise_handles_trailing_punctuation():
    # Real-world: URLs glued onto the end of a sentence with a comma or period.
    out = normalise_url("https://procontract.due-north.com/tenders/123,")
    assert out is not None
    norm, _ = out
    assert norm.endswith("/tenders/123")


def test_normalise_rejects_non_http_schemes():
    assert normalise_url("ftp://files.example.com/foo") is None
    assert normalise_url("javascript:alert(1)") is None
    assert normalise_url("mailto:ops@example.com") is None


def test_normalise_rejects_garbage():
    assert normalise_url("not a url at all") is None
    assert normalise_url("") is None


# --- gov.uk apex special case ------------------------------------------


def test_gov_uk_apex_is_apex():
    assert is_gov_uk_apex("gov.uk") is True


def test_gov_uk_subdomains_are_not_apex():
    assert is_gov_uk_apex("nepo.gov.uk") is False
    assert is_gov_uk_apex("crowncommercial.gov.uk") is False
    assert is_gov_uk_apex("procurement.nhs.gov.uk") is False
