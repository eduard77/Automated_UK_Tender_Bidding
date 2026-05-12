"""Document downloader.

Pulls tender attachment URLs (ITT, spec, pricing schedules, etc.) and stores
them locally (Pass 2) or to S3 (Pass 3+). For PDFs and DOCX, attempts text
extraction so the requirements extractor has plain text to work with.

Storage layout (local backend):
    {DOCUMENT_STORAGE_DIR}/{tender_id}/{sha256[:2]}/{sha256}{.ext}
"""
from __future__ import annotations

import hashlib
import io
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import Tender, TenderDocumentFile

logger = structlog.get_logger(__name__)


def _safe_ext(format_hint: str | None, url: str) -> str:
    if format_hint:
        f = format_hint.lower().strip().lstrip(".")
        if f and len(f) <= 8 and f.isalnum():
            return f
    path = urlparse(url).path
    if "." in path:
        candidate = path.rsplit(".", 1)[-1].lower()
        if candidate.isalnum() and len(candidate) <= 8:
            return candidate
    return "bin"


def _storage_path(tender_id: int, sha256: str, ext: str) -> Path:
    base = Path(settings.document_storage_dir)
    return base / str(tender_id) / sha256[:2] / f"{sha256}.{ext}"


async def _fetch(url: str) -> tuple[bytes, str | None]:
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    ) as client, client.stream("GET", url) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip() or None
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > settings.document_max_bytes:
                raise ValueError(f"document exceeds max size {settings.document_max_bytes}")
        return bytes(buf), content_type


def _extract_text(data: bytes, ext: str) -> str | None:
    """Best-effort text extraction. Returns None if not extractable here."""
    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return None
        try:
            reader = PdfReader(io.BytesIO(data))
            parts = []
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001
                    continue
            return "\n".join(parts).strip() or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdf.extract_failed", error=str(exc))
            return None
    if ext in {"docx", "doc"}:
        try:
            from docx import Document
        except ImportError:
            return None
        try:
            doc = Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs) or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("docx.extract_failed", error=str(exc))
            return None
    if ext in {"txt", "md", "csv"}:
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    return None


async def download_documents_for_tender(db: Session, tender: Tender) -> list[TenderDocumentFile]:
    """Download every document referenced by the tender, recording results in DB."""
    if not tender.documents:
        return []

    results: list[TenderDocumentFile] = []
    for doc in tender.documents:
        url = doc.get("url") if isinstance(doc, dict) else None
        if not url:
            continue
        title = doc.get("title") if isinstance(doc, dict) else None
        format_hint = doc.get("format") if isinstance(doc, dict) else None

        existing = (
            db.query(TenderDocumentFile)
            .filter(TenderDocumentFile.tender_id == tender.id, TenderDocumentFile.url == url)
            .one_or_none()
        )
        if existing and existing.download_status == "ok":
            results.append(existing)
            continue

        record = existing or TenderDocumentFile(
            tender_id=tender.id, url=url, title=title, format=format_hint
        )
        if existing is None:
            db.add(record)
            db.flush()

        try:
            data, content_type = await _fetch(url)
            sha = hashlib.sha256(data).hexdigest()
            ext = _safe_ext(format_hint or content_type, url)
            target = _storage_path(tender.id, sha, ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(data)
            record.storage_key = str(target.relative_to(Path(settings.document_storage_dir)))
            record.storage_backend = "local"
            record.bytes = len(data)
            record.sha256 = sha
            record.text_extracted = _extract_text(data, ext)
            record.download_status = "ok"
            record.error = None
            record.downloaded_at = datetime.now(UTC)
        except Exception as exc:  # noqa: BLE001
            record.download_status = "error"
            record.error = f"{type(exc).__name__}: {exc}"
            logger.warning("doc.download_failed", url=url, error=str(exc))

        db.commit()
        results.append(record)

    return results


def aggregate_text(files: list[TenderDocumentFile], max_chars: int = 80_000) -> str:
    """Concatenate extracted text across documents, capped to max_chars."""
    parts = []
    used = 0
    for f in files:
        if not f.text_extracted:
            continue
        header = f"\n\n=== {f.title or os.path.basename(f.url)} ===\n"
        chunk = (header + f.text_extracted).strip()
        if used + len(chunk) > max_chars:
            chunk = chunk[: max_chars - used]
            parts.append(chunk)
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)
