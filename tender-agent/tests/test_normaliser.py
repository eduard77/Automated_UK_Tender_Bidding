from decimal import Decimal

from tender_agent.services.normaliser import ocds_release_to_tender


def test_ocds_minimal_release_normalises():
    release = {
        "ocid": "ocds-h6vhtk-XYZ123",
        "id": "release-1",
        "date": "2025-09-12T10:00:00Z",
        "tag": ["tender"],
        "tender": {
            "id": "PROC-2025-001",
            "title": "Cleaning services for community centres",
            "description": "Multi-site cleaning contract.",
            "status": "active",
            "value": {"amount": "250000.00", "currency": "GBP"},
            "tenderPeriod": {"endDate": "2025-10-15T17:00:00Z"},
            "classification": {"scheme": "CPV", "id": "90910000"},
            "items": [
                {
                    "id": "1",
                    "classification": {"scheme": "CPV", "id": "90919200"},
                }
            ],
            "documents": [
                {"title": "ITT", "url": "https://example.gov.uk/itt.pdf", "format": "pdf"}
            ],
        },
        "buyer": {"id": "GB-COH-12345", "name": "Some Council"},
        "parties": [
            {
                "id": "GB-COH-12345",
                "name": "Some Council",
                "address": {"countryName": "United Kingdom", "region": "South West"},
            }
        ],
    }

    t = ocds_release_to_tender(release, source_code="FTS")

    assert t.source_code == "FTS"
    assert t.source_ref == "ocds-h6vhtk-XYZ123"
    assert t.procurement_ref == "PROC-2025-001"
    assert t.title.startswith("Cleaning services")
    assert t.status == "active"
    assert t.notice_type == "tender"
    assert t.buyer_name == "Some Council"
    assert t.buyer_country == "United Kingdom"
    assert t.value_amount == Decimal("250000.00")
    assert t.value_currency == "GBP"
    assert "90910000" in t.cpv_codes
    assert "90919200" in t.cpv_codes
    assert t.deadline_at is not None and t.deadline_at.year == 2025
    assert len(t.documents) == 1
    assert t.documents[0].url == "https://example.gov.uk/itt.pdf"


def test_ocds_release_missing_ocid_raises():
    import pytest

    with pytest.raises(ValueError):
        ocds_release_to_tender({"tender": {}}, source_code="FTS")


def test_status_mapping():
    release = {
        "ocid": "x",
        "tender": {"title": "T", "status": "complete"},
    }
    t = ocds_release_to_tender(release, source_code="FTS")
    assert t.status == "closed"
