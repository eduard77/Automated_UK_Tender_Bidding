"""Pure HTML extractors for the dashboard.

Why a parser at all when Playwright can query the DOM live? Two reasons:

1. The bot's UNIT tests must run anywhere — they pin "given this DOM,
   extract these results / sources" without Playwright or a browser.
   When a future redesign moves a selector, the unit test fails locally,
   not 30s into a cloud run (the same discipline that caught the
   Proactis chain).
2. The live driver feeds these extractors `page.content()` (rendered
   HTML) so the LIVE path and the UNIT path read the SAME markup with
   the SAME logic — they cannot drift.

The selectors here mirror the dashboard's SearchPage / TenderList
components (real source of truth: tender-agent-dashboard/components/
SearchPage.tsx + TenderList.tsx). Each result card is an `<article>`
inside `<ul>`; the source code lives on a `<span title="<code>">` so we
read the canonical code, not the friendly label.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A `<span title="CODE">FRIENDLY</span>` carrying the source_code on its
# `title` attribute — SearchPage's ResultCard renders this exactly. Order
# of attributes is variable so we go attribute-by-attribute.
_RESULT_ARTICLE_RE = re.compile(
    r"<article\b[^>]*>(?P<body>.*?)</article>", re.IGNORECASE | re.DOTALL
)
_SOURCE_SPAN_RE = re.compile(
    r'<span\b[^>]*\btitle\s*=\s*"(?P<code>[^"]+)"[^>]*>'
    r"(?P<label>[^<]*)</span>",
    re.IGNORECASE,
)
_TITLE_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*"/tenders/(?P<id>\d+)"[^>]*>'
    r"(?P<title>[^<]*)</a>",
    re.IGNORECASE,
)
# Source-chip buttons inside the "Source (default: all)" Fieldset. The
# chip's button has `aria-pressed` and its label text is the friendly
# name; we count visible chips, not pressed state.
_CHIP_BUTTON_RE = re.compile(
    r'<button\b[^>]*\baria-pressed\s*=\s*"(?P<pressed>true|false)"'
    r"[^>]*>(?P<label>[^<]+)</button>",
    re.IGNORECASE,
)
# A CPV row on the tender DETAIL page. The Tender API serialises
# cpv_codes; the dashboard's detail page lists them under a "CPV" heading
# / cell. We don't pin a specific element shape (the detail page wasn't
# in scope for Phase 0's seed selectors) — we look for "CPV" header text
# followed by a digit run within a small window. The unit test fixture
# uses the real markup the operator confirms.
_CPV_CELL_RE = re.compile(
    r">\s*CPV\b[^<]*</[a-z0-9]+>\s*<[a-z0-9]+[^>]*>\s*(?P<value>[^<]+?)\s*<",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchResult:
    """One result card as the bot reads it from the rendered DOM.

    `source_code` is the platform-canonical code (CF / FTS / PCS /
    PROACTIS / SAMPLE_SEED / EU_SUPPLY / ATAMIS / S2W / NI). `title` is
    the visible heading link text. `tender_id` is the integer id used by
    the detail-page link (so the OpenTender step can deep-link without
    re-traversing the list).
    """

    tender_id: int
    title: str
    source_code: str


def extract_results_from_dom(html: str) -> list[SearchResult]:
    """Pull every result `<article>` out of a rendered SearchPage and
    return them in display order. Returns `[]` for the empty-state DOM
    ('No tenders match these filters.') — the empty case is a verdict the
    bot reports, not an error."""
    out: list[SearchResult] = []
    for article in _RESULT_ARTICLE_RE.finditer(html or ""):
        body = article.group("body")
        source_match = _SOURCE_SPAN_RE.search(body)
        title_match = _TITLE_LINK_RE.search(body)
        if source_match is None or title_match is None:
            continue
        out.append(
            SearchResult(
                tender_id=int(title_match.group("id")),
                title=title_match.group("title").strip(),
                source_code=source_match.group("code").strip(),
            )
        )
    return out


def extract_source_chip_options(html: str) -> list[str]:
    """The friendly labels of every Source chip rendered in the filter
    panel. Phase 0 uses this to assert which sources currently exist in
    the Source facet (the handover's `known current state`). Returns
    labels in DOM order; deduplicates to be tolerant of partial rerenders.
    """
    seen: dict[str, None] = {}
    for match in _CHIP_BUTTON_RE.finditer(html or ""):
        label = match.group("label").strip()
        if label:
            seen.setdefault(label, None)
    return list(seen)


def extract_cpv_cell(html: str) -> str | None:
    """Read the CPV value from a tender detail page. Returns the
    stringified value (could be an empty cell "—" for a Proactis tender
    today). Returns `None` only when no CPV row exists at all. The unit
    fixture pins this against the real detail-page markup."""
    match = _CPV_CELL_RE.search(html or "")
    if match is None:
        return None
    return match.group("value").strip() or None
