#!/usr/bin/env python3
"""Seed 8 PLACEHOLDER vault documents for dashboard development.

Inserts eight VaultDocument + VaultDocumentVersion rows with realistic but
fictional claims so the dashboard's /vault page renders with structure on a
fresh database. Every title is prefixed `PLACEHOLDER -- ` so the UI's
placeholder banner catches them.

Storage: writes a small plain-text file per version under
`settings.document_storage_dir` (default `/var/tender-agent/documents`).
Inside the container that path is writable; outside it falls back to a
temporary directory if not writable.

Safety:
    Refuses to run if `TENDER_AGENT_ENV=production`. Never run this against a
    production database — it polutes the vault.

Usage:
    docker compose exec -T --user root app /opt/venv/bin/python \\
        /app/scripts/seed_vault_placeholders.py

    # or, from the host with a local DATABASE_URL:
    python scripts/seed_vault_placeholders.py
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

# Make `tender_agent` importable when run from the repo root or scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402

from tender_agent.db import SessionLocal  # noqa: E402
from tender_agent.models import VaultDocument, VaultDocumentVersion  # noqa: E402


PLACEHOLDER_PREFIX = "PLACEHOLDER -- "


def _placeholder_text(title: str, claims: dict) -> str:
    """Cheap stand-in for a real PDF/DOCX. Includes the title and a JSON-ish
    rendering of the claims so a human opening the file can see what the
    placeholder represents."""
    lines = [
        title,
        "=" * len(title),
        "",
        "This is a PLACEHOLDER document seeded for dashboard testing.",
        "Replace with real evidence before relying on bid assessments.",
        "",
        "Claimed values:",
    ]
    for k, v in claims.items():
        if isinstance(v, list):
            v = ", ".join(map(str, v)) if v else "(empty)"
        lines.append(f"  - {k}: {v}")
    return "\n".join(lines) + "\n"


def _write_blob(root: Path, *, document_id: int, version: int, body: str) -> tuple[str, int, str]:
    """Write the placeholder text to disk and return (storage_key, bytes, sha256).

    `storage_key` mirrors the production layout: vault/{org_id}/{doc_id}/{ver}.txt
    """
    import hashlib

    key = f"vault/1/{document_id}/{version}.txt"
    full_path = root / key
    full_path.parent.mkdir(parents=True, exist_ok=True)
    content = body.encode("utf-8")
    full_path.write_bytes(content)
    return key, len(content), hashlib.sha256(content).hexdigest()


def _placeholders() -> list[dict]:
    """The eight placeholder definitions. Fictional but plausible values.

    Date fields stored as ISO strings (will be parsed to `date` objects when
    inserted). Mirror columns on the version row pick up expiry/issued/body
    so the matcher's WHERE filters work without unpacking JSON.
    """
    return [
        # 1. Professional Indemnity
        {
            "category": "insurance",
            "subcategory": "professional_indemnity",
            "title": f"{PLACEHOLDER_PREFIX}Professional Indemnity Insurance",
            "expiry_date": date(2027, 1, 1),
            "issuing_body": "Hiscox UK",
            "issued_date": date(2025, 1, 1),
            "claims": {
                "doc_type": "insurance_certificate",
                "insurance_type": "professional_indemnity",
                "cover_amount": "10000000.00",
                "currency": "GBP",
                "insurer": "Hiscox UK",
                "insurer_uk_authorised": True,
                "policy_holder": "Genera Systems Ltd",
                "policy_number": "PI-PLC-001",
                "valid_from": "2025-01-01",
                "valid_until": "2027-01-01",
                "territory": "UK",
                "low_confidence_fields": [],
                "notes": ["Placeholder seeded for dashboard testing."],
            },
        },
        # 2. Employers Liability
        {
            "category": "insurance",
            "subcategory": "employers_liability",
            "title": f"{PLACEHOLDER_PREFIX}Employers Liability Insurance",
            "expiry_date": date(2027, 4, 1),
            "issuing_body": "AXA UK",
            "issued_date": date(2025, 4, 1),
            "claims": {
                "doc_type": "insurance_certificate",
                "insurance_type": "employers_liability",
                "cover_amount": "10000000.00",
                "currency": "GBP",
                "insurer": "AXA UK",
                "insurer_uk_authorised": True,
                "policy_holder": "Genera Systems Ltd",
                "policy_number": "EL-PLC-002",
                "valid_from": "2025-04-01",
                "valid_until": "2027-04-01",
                "territory": "UK",
                "low_confidence_fields": [],
                "notes": ["Placeholder seeded for dashboard testing."],
            },
        },
        # 3. Public Liability
        {
            "category": "insurance",
            "subcategory": "public_liability",
            "title": f"{PLACEHOLDER_PREFIX}Public Liability Insurance",
            "expiry_date": date(2027, 4, 1),
            "issuing_body": "Aviva",
            "issued_date": date(2025, 4, 1),
            "claims": {
                "doc_type": "insurance_certificate",
                "insurance_type": "public_liability",
                "cover_amount": "5000000.00",
                "currency": "GBP",
                "insurer": "Aviva",
                "insurer_uk_authorised": True,
                "policy_holder": "Genera Systems Ltd",
                "policy_number": "PL-PLC-003",
                "valid_from": "2025-04-01",
                "valid_until": "2027-04-01",
                "territory": "UK",
                "low_confidence_fields": [],
                "notes": ["Placeholder seeded for dashboard testing."],
            },
        },
        # 4. ISO 9001
        {
            "category": "accreditation",
            "subcategory": "iso_9001",
            "title": f"{PLACEHOLDER_PREFIX}ISO 9001:2015 Quality Management",
            "expiry_date": date(2027, 6, 1),
            "issuing_body": "BSI",
            "issued_date": date(2024, 6, 1),
            "claims": {
                "doc_type": "iso_certificate",
                "standard": "ISO 9001",
                "standard_version": "2015",
                "scope": "Construction project management",
                "certifying_body": "BSI",
                "certificate_number": "ABC-12345",
                "issued_date": "2024-06-01",
                "valid_until": "2027-06-01",
                "holder": "Genera Systems Ltd",
                "low_confidence_fields": [],
                "notes": ["Placeholder seeded for dashboard testing."],
            },
        },
        # 5. ISO 14001
        {
            "category": "accreditation",
            "subcategory": "iso_14001",
            "title": f"{PLACEHOLDER_PREFIX}ISO 14001:2015 Environmental",
            "expiry_date": date(2027, 6, 1),
            "issuing_body": "BSI",
            "issued_date": date(2024, 6, 1),
            "claims": {
                "doc_type": "iso_certificate",
                "standard": "ISO 14001",
                "standard_version": "2015",
                "scope": "Construction project management",
                "certifying_body": "BSI",
                "certificate_number": "ABC-23456",
                "issued_date": "2024-06-01",
                "valid_until": "2027-06-01",
                "holder": "Genera Systems Ltd",
                "low_confidence_fields": [],
                "notes": ["Placeholder seeded for dashboard testing."],
            },
        },
        # 6. ISO 45001
        {
            "category": "accreditation",
            "subcategory": "iso_45001",
            "title": f"{PLACEHOLDER_PREFIX}ISO 45001:2018 Health & Safety",
            "expiry_date": date(2027, 6, 1),
            "issuing_body": "BSI",
            "issued_date": date(2024, 6, 1),
            "claims": {
                "doc_type": "iso_certificate",
                "standard": "ISO 45001",
                "standard_version": "2018",
                "scope": "Construction project management",
                "certifying_body": "BSI",
                "certificate_number": "ABC-34567",
                "issued_date": "2024-06-01",
                "valid_until": "2027-06-01",
                "holder": "Genera Systems Ltd",
                "low_confidence_fields": [],
                "notes": ["Placeholder seeded for dashboard testing."],
            },
        },
        # 7. ISO 19650
        {
            "category": "accreditation",
            "subcategory": "iso_19650",
            "title": f"{PLACEHOLDER_PREFIX}ISO 19650:2018 BIM Information Management",
            "expiry_date": date(2027, 9, 1),
            "issuing_body": "BSI",
            "issued_date": date(2024, 9, 1),
            "claims": {
                "doc_type": "iso_certificate",
                "standard": "ISO 19650",
                "standard_version": "2018",
                "scope": "BIM information management on construction projects",
                "certifying_body": "BSI",
                "certificate_number": "ABC-45678",
                "issued_date": "2024-09-01",
                "valid_until": "2027-09-01",
                "holder": "Genera Systems Ltd",
                "low_confidence_fields": [],
                "notes": ["Placeholder seeded for dashboard testing."],
            },
        },
        # 8. Annual accounts
        {
            "category": "financial",
            "subcategory": "annual_accounts",
            "title": f"{PLACEHOLDER_PREFIX}Annual accounts, Y/E 2024",
            "expiry_date": None,
            "issuing_body": None,
            "issued_date": date(2024, 12, 31),
            "claims": {
                "doc_type": "accounts",
                "fiscal_year_end": "2024-12-31",
                "turnover": "2500000.00",
                "currency": "GBP",
                "profit_before_tax": "180000.00",
                "audited": False,
                "auditor": None,
                "low_confidence_fields": [],
                "notes": [
                    "Placeholder seeded for dashboard testing.",
                    "Net assets approx GBP 450,000; no qualifications.",
                ],
            },
        },
    ]


def _resolve_storage_root() -> Path:
    """Pick a writable directory for placeholder blobs.

    Prefer `settings.document_storage_dir`; fall back to a temp directory if
    the configured path isn't writable (eg running on Windows host where the
    default `/var/tender-agent/documents` doesn't exist).
    """
    from tender_agent.config import settings

    root = Path(settings.document_storage_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Touch a probe to confirm writability.
        probe = root / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        return root
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "tender-agent-vault-placeholders"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-seed even if PLACEHOLDER documents already exist (deletes the old ones first).",
    )
    args = parser.parse_args()

    if os.environ.get("TENDER_AGENT_ENV", "").lower() == "production":
        print("Refusing to run: TENDER_AGENT_ENV=production")
        return 2

    storage_root = _resolve_storage_root()
    print(f"Storage root: {storage_root}")

    with SessionLocal() as db:
        existing = (
            db.execute(
                select(VaultDocument).where(
                    VaultDocument.title.like(f"{PLACEHOLDER_PREFIX}%")
                )
            )
            .scalars()
            .all()
        )

        if existing and not args.force:
            print(
                f"Found {len(existing)} existing PLACEHOLDER document(s). "
                f"Skipping (pass --force to re-seed)."
            )
            for d in existing:
                print(f"  id={d.id}  {d.title}")
            return 0

        if existing and args.force:
            for d in existing:
                db.delete(d)
            db.flush()
            print(f"Cleared {len(existing)} existing PLACEHOLDER document(s).")

        created: list[tuple[int, str]] = []
        for entry in _placeholders():
            doc = VaultDocument(
                category=entry["category"],
                subcategory=entry["subcategory"],
                title=entry["title"],
            )
            db.add(doc)
            db.flush()  # assign doc.id

            body = _placeholder_text(entry["title"], entry["claims"])
            storage_key, bytes_written, sha = _write_blob(
                storage_root,
                document_id=doc.id,
                version=1,
                body=body,
            )

            version = VaultDocumentVersion(
                document_id=doc.id,
                version=1,
                storage_key=storage_key,
                bytes=bytes_written,
                sha256=sha,
                mime_type="text/plain",
                title=entry["title"],
                claims=entry["claims"],
                claims_confirmed=True,
                text_extracted=body,
                expiry_date=entry["expiry_date"],
                issuing_body=entry["issuing_body"],
                issued_date=entry["issued_date"],
                uploaded_by="seed_vault_placeholders.py",
                uploaded_at=datetime.now(UTC),
            )
            db.add(version)
            db.flush()

            doc.current_version_id = version.id
            db.flush()

            created.append((doc.id, doc.title))
            print(
                f"  + id={doc.id:>3}  category={doc.category:<14}  {doc.title}"
            )

        db.commit()
        print(f"\nSeeded {len(created)} placeholder vault document(s).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
