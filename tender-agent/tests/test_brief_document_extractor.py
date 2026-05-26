"""Per-file text extraction (chunk 5 Part A).

Exercises the extractor on docx / multi-sheet xlsx / pdf with and without text /
zip recursion / unsupported / corrupt files. The contract is: never raise,
always return a populated status.
"""
from __future__ import annotations

import io
import zipfile

from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter

from tender_agent.services.brief.document_extractor import (
    ExtractedDoc,
    extract_from_bytes,
    iter_zip_members,
    resolve_storage_path,
)


def _make_docx_bytes(paragraphs: list[str], tables: list[list[list[str]]] | None = None) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    for tbl in tables or []:
        rows = len(tbl)
        cols = len(tbl[0]) if rows else 0
        tbl_obj = doc.add_table(rows=rows, cols=cols)
        for r, row in enumerate(tbl):
            for c, cell in enumerate(row):
                tbl_obj.rows[r].cells[c].text = cell
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_empty_pdf_bytes() -> bytes:
    """A minimal, valid PDF with no text content — exercises the 'scanned PDF'
    (status='empty') path without flake."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_extract_txt_ok() -> None:
    r = extract_from_bytes(b"hello world\nsecond line", "txt")
    assert r.status == "ok"
    assert r.text is not None
    assert "hello" in r.text
    assert r.doc_type == "txt"
    assert r.char_count == len("hello world\nsecond line")


def test_extract_txt_empty_is_empty_not_error() -> None:
    r = extract_from_bytes(b"   \n  ", "txt")
    assert r.status == "empty"
    assert r.text is None


def test_extract_csv_ok() -> None:
    r = extract_from_bytes(b"a,b,c\n1,2,3\n", "csv")
    assert r.status == "ok"
    assert "1,2,3" in (r.text or "")


def test_extract_docx_paragraphs_and_tables() -> None:
    data = _make_docx_bytes(
        paragraphs=["Project: Build a school", "Buyer is Acme Ltd."],
        tables=[[["Requirement", "Weight"], ["Insurance £5m", "60%"]]],
    )
    r = extract_from_bytes(data, "docx")
    assert r.status == "ok"
    assert r.doc_type == "docx"
    text = r.text or ""
    assert "Project: Build a school" in text
    assert "Insurance £5m" in text
    assert "60%" in text


def test_extract_xlsx_multi_sheet_with_values() -> None:
    data = _make_xlsx_bytes(
        {
            "Pricing": [["Item", "Qty", "Price"], ["Pump", 2, 1500.0]],
            "Schedule": [["Phase", "Weeks"], ["Build", 12]],
        }
    )
    r = extract_from_bytes(data, "xlsx")
    assert r.status == "ok"
    assert r.doc_type == "xlsx"
    text = r.text or ""
    assert "## sheet: Pricing" in text
    assert "Pump" in text
    assert "## sheet: Schedule" in text
    assert "Build" in text


def test_extract_xlsx_empty_workbook_is_empty() -> None:
    data = _make_xlsx_bytes({"Empty": []})
    r = extract_from_bytes(data, "xlsx")
    assert r.status == "empty"


def test_extract_pdf_blank_pages_is_empty_not_error() -> None:
    data = _make_empty_pdf_bytes()
    r = extract_from_bytes(data, "pdf")
    # Some extractors return ok with empty string, but our extractor returns
    # 'empty' when no extractable text is produced — that's the contract.
    assert r.status == "empty"
    assert r.doc_type == "pdf"


def test_extract_corrupt_pdf_is_error_not_throw() -> None:
    r = extract_from_bytes(b"not a real pdf %%%", "pdf")
    # Either pdfplumber/pypdf reports an error or empty — must NOT raise.
    assert r.status in {"error", "empty"}


def test_extract_unsupported_extension() -> None:
    r = extract_from_bytes(b"\x01\x02\x03", "dwg")
    assert r.status == "unsupported"
    assert r.doc_type == "dwg"


def test_extract_zip_recurses_into_members() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("instructions.txt", b"Tender instructions here.")
        zf.writestr(
            "spec.docx",
            _make_docx_bytes(paragraphs=["Spec requires concrete grade C40."]),
        )
        zf.writestr("notes.dwg", b"\x00\x00")  # unsupported member, must not fail
    r = extract_from_bytes(buf.getvalue(), "zip")
    assert r.status == "ok"
    text = r.text or ""
    assert "Tender instructions here." in text
    assert "Spec requires concrete grade C40." in text


def test_iter_zip_members_returns_per_member_results(tmp_path) -> None:
    target = tmp_path / "pack.zip"
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("readme.txt", b"hello")
        zf.writestr("unknown.bin", b"\x00\x00")
    # iter_zip_members reads through resolve_storage_path; use an absolute
    # storage_key.
    members = iter_zip_members(str(target))
    names = [n for (n, _) in members]
    assert "readme.txt" in names
    assert "unknown.bin" in names
    by_name = dict(members)
    assert by_name["readme.txt"].status == "ok"
    assert by_name["unknown.bin"].status == "unsupported"


def test_extract_from_bytes_does_not_raise_on_completely_random_data() -> None:
    # Belt & braces: any unknown ext returns 'unsupported' without exception.
    for ext in ["xyz", "skp", ""]:
        r = extract_from_bytes(b"\x00\xFF\x42garbage", ext or "bin")
        assert isinstance(r, ExtractedDoc)
        assert r.status in {"unsupported", "error", "empty"}


def test_resolve_storage_path_supports_absolute(tmp_path) -> None:
    p = resolve_storage_path(str(tmp_path / "x.txt"))
    assert str(p).endswith("x.txt")
