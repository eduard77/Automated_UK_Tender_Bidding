"""Per-file text extraction for the brief engine (chunk 5).

Input: a TenderDocumentFile (path on disk). Output: an ExtractedDoc with text +
status. Never raises on a bad file — the caller must be able to process the
rest of the pack even if one document is corrupt or unsupported.

Status values:
  ok          — extracted_text populated.
  empty       — file readable but produced no text (scanned PDF, blank docx).
  unsupported — extension we don't extract (.dwg, .skp, …).
  error       — extractor crashed (corrupt file, etc.); detail describes it.
"""
from __future__ import annotations

import contextlib
import io
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from tender_agent.config import settings

logger = structlog.get_logger(__name__)

# Bumping the version re-extracts every document on the next ensure_content
# call. Use this when extraction logic changes meaningfully.
EXTRACTOR_VERSION = "v1"

# What we know how to read.
_TEXTUAL_EXTS = {"txt", "md", "csv", "log"}
_DOCX_EXTS = {"docx"}
_XLSX_EXTS = {"xlsx"}
_PDF_EXTS = {"pdf"}
_ZIP_EXTS = {"zip"}


@dataclass(frozen=True)
class ExtractedDoc:
    text: str | None
    char_count: int
    status: str
    detail: str | None
    doc_type: str | None

    @classmethod
    def ok(cls, text: str, doc_type: str) -> ExtractedDoc:
        return cls(
            text=text,
            char_count=len(text),
            status="ok",
            detail=None,
            doc_type=doc_type,
        )

    @classmethod
    def empty(cls, doc_type: str, detail: str) -> ExtractedDoc:
        return cls(
            text=None, char_count=0, status="empty", detail=detail, doc_type=doc_type
        )

    @classmethod
    def unsupported(cls, doc_type: str | None, detail: str) -> ExtractedDoc:
        return cls(
            text=None,
            char_count=0,
            status="unsupported",
            detail=detail,
            doc_type=doc_type,
        )

    @classmethod
    def error(cls, doc_type: str | None, detail: str) -> ExtractedDoc:
        return cls(
            text=None, char_count=0, status="error", detail=detail, doc_type=doc_type
        )


def _ext_from(filename: str | None, fallback_format: str | None) -> str:
    if filename and "." in filename:
        e = filename.rsplit(".", 1)[-1].lower()
        if e.isalnum():
            return e
    if fallback_format:
        e = fallback_format.lower().lstrip(".")
        if e.isalnum():
            return e
    return ""


def resolve_storage_path(storage_key: str) -> Path:
    """Resolve a TenderDocumentFile.storage_key to an absolute on-disk path.

    The orchestrator uses the configured document_storage_dir as the root;
    storage_key is relative when the bridge/CF adapter persisted it, but a few
    older paths are absolute. Either is fine."""
    p = Path(storage_key)
    if p.is_absolute():
        return p
    return Path(settings.document_storage_dir) / p


def extract_from_bytes(
    data: bytes, ext: str, filename: str | None = None
) -> ExtractedDoc:
    """Pure-bytes extraction. Used by zip-member recursion and tests."""
    ext = (ext or "").lower().lstrip(".")
    name = filename or f"<inline.{ext or 'bin'}>"
    if ext in _TEXTUAL_EXTS:
        return _extract_textual(data, ext)
    if ext in _DOCX_EXTS:
        return _extract_docx(data)
    if ext in _XLSX_EXTS:
        return _extract_xlsx(data)
    if ext in _PDF_EXTS:
        return _extract_pdf(data)
    if ext in _ZIP_EXTS:
        return _extract_zip(data)
    return ExtractedDoc.unsupported(ext or None, f"no extractor for {name}")


def extract_document(
    storage_key: str,
    filename: str | None,
    format_hint: str | None = None,
) -> ExtractedDoc:
    """Read storage_key off disk and extract. Never raises."""
    path = resolve_storage_path(storage_key)
    if not path.is_file():
        return ExtractedDoc.error(
            _ext_from(filename, format_hint) or None,
            f"file not found on disk: {storage_key}",
        )
    try:
        data = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error(
            _ext_from(filename, format_hint) or None,
            f"read failed: {type(exc).__name__}: {exc}",
        )
    ext = _ext_from(filename, format_hint)
    return extract_from_bytes(data, ext, filename=filename)


# --- format-specific helpers --------------------------------------------------


def _extract_textual(data: bytes, ext: str) -> ExtractedDoc:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error(ext, f"decode failed: {exc}")
    if not text.strip():
        return ExtractedDoc.empty(ext, "no textual content")
    return ExtractedDoc.ok(text, ext)


def _extract_docx(data: bytes) -> ExtractedDoc:
    try:
        from docx import Document
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error("docx", f"python-docx not available: {exc}")
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error("docx", f"open failed: {type(exc).__name__}: {exc}")
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text:
            parts.append(p.text)
    # Tables — flatten cells row by row so requirements grids survive.
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        return ExtractedDoc.empty("docx", "no textual content")
    return ExtractedDoc.ok(text, "docx")


def _extract_xlsx(data: bytes) -> ExtractedDoc:
    try:
        from openpyxl import load_workbook
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error("xlsx", f"openpyxl not available: {exc}")
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error("xlsx", f"open failed: {type(exc).__name__}: {exc}")
    sheet_blocks: list[str] = []
    for ws in wb.worksheets:
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = [
                "" if v is None else str(v).strip() for v in row
            ]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            sheet_blocks.append(f"## sheet: {ws.title}\n" + "\n".join(rows))
    with contextlib.suppress(Exception):
        wb.close()
    text = "\n\n".join(sheet_blocks).strip()
    if not text:
        return ExtractedDoc.empty("xlsx", "workbook has no values")
    return ExtractedDoc.ok(text, "xlsx")


def _extract_pdf(data: bytes) -> ExtractedDoc:
    # pdfplumber is preferred (better layout heuristics); pypdf is the fallback
    # (already in deps for the requirements extractor).
    try:
        import pdfplumber  # type: ignore[import-untyped]
    except Exception:  # noqa: BLE001
        pdfplumber = None  # type: ignore[assignment]
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                parts = []
                for page in pdf.pages:
                    try:
                        t = page.extract_text() or ""
                    except Exception:  # noqa: BLE001
                        t = ""
                    if t.strip():
                        parts.append(t)
            text = "\n".join(parts).strip()
            if text:
                return ExtractedDoc.ok(text, "pdf")
            return ExtractedDoc.empty(
                "pdf", "no extractable text (likely scanned)"
            )
        except Exception as exc:  # noqa: BLE001
            # Fall through to pypdf rather than declare error immediately.
            logger.info("brief.pdfplumber_failed", error=str(exc))
    # pypdf fallback.
    try:
        from pypdf import PdfReader
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error("pdf", f"no PDF reader available: {exc}")
    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                continue
        text = "\n".join(parts).strip()
        if not text:
            return ExtractedDoc.empty(
                "pdf", "no extractable text (likely scanned)"
            )
        return ExtractedDoc.ok(text, "pdf")
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error("pdf", f"pypdf failed: {type(exc).__name__}: {exc}")


def _extract_zip(data: bytes) -> ExtractedDoc:
    """Aggregate text from members. One row per member is handled by the
    content store; here we produce a single concatenated text view used for
    the parent file (so the zip itself isn't recorded as 'empty')."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        return ExtractedDoc.error("zip", f"open failed: {type(exc).__name__}: {exc}")
    blocks: list[str] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        member_name = info.filename
        ext = (
            member_name.rsplit(".", 1)[-1].lower()
            if "." in member_name
            else ""
        )
        try:
            payload = zf.read(info)
        except Exception as exc:  # noqa: BLE001
            blocks.append(f"## {member_name}\n[read error: {exc}]")
            continue
        sub = extract_from_bytes(payload, ext, filename=member_name)
        if sub.status == "ok" and sub.text:
            blocks.append(f"## {member_name}\n{sub.text}")
    zf.close()
    text = "\n\n".join(blocks).strip()
    if not text:
        return ExtractedDoc.empty("zip", "zip contained no extractable members")
    return ExtractedDoc.ok(text, "zip")


def iter_zip_members(storage_key: str) -> list[tuple[str, ExtractedDoc]]:
    """Walk a zip on disk and produce (member_name, ExtractedDoc) for each
    file member. Used by the content store to record per-member content rows.
    """
    path = resolve_storage_path(storage_key)
    if not path.is_file():
        return []
    try:
        data = path.read_bytes()
    except Exception:  # noqa: BLE001
        return []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, ExtractedDoc]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        ext = (
            info.filename.rsplit(".", 1)[-1].lower()
            if "." in info.filename
            else ""
        )
        try:
            payload = zf.read(info)
        except Exception as exc:  # noqa: BLE001
            out.append(
                (info.filename, ExtractedDoc.error(ext or None, f"read failed: {exc}"))
            )
            continue
        out.append((info.filename, extract_from_bytes(payload, ext, info.filename)))
    zf.close()
    return out


# Re-exported so callers don't import from os everywhere.
basename = os.path.basename
