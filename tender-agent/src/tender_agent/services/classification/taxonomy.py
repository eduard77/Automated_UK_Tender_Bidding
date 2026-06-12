"""The fixed classification taxonomy + validation + the cached system prompt.

This module is pure data + pure functions — no I/O, no DB, no Anthropic — so
it imports cleanly into tests and into both the per-call and Batch-API paths.

Design notes:
- The taxonomy is FIXED and identical on every call, so it lives in the
  *cached* system prompt (Anthropic prompt caching) — see `system_prompt_blocks`.
- `CLASSIFIER_VERSION` stamps every classified row. Bump it whenever the
  taxonomy OR the prompt OR the parsing changes, so a re-run reclassifies only
  what's stale (the backfill / worker skip rows already on the current version).
- Sub-categories are stored per-sector in a dict (`subcategories` JSON column),
  so other sectors' sub-lists can be added later WITHOUT a schema change —
  only Construction has a sub-list today.
- Matching is normalisation-tolerant (case / dash / whitespace) but only ever
  STORES a canonical value from the lists below.
"""
from __future__ import annotations

import re

#: Bump when taxonomy, prompt, or parsing changes — drives idempotent re-runs.
CLASSIFIER_VERSION = "2026-06-12.haiku45.v1"

OTHER_SECTOR = "Other / Uncategorised"
CONSTRUCTION_SECTOR = "Construction & Built Environment"

#: Top-level sector — assigned to EVERY tender (exactly one primary).
SECTORS: tuple[str, ...] = (
    "Construction & Built Environment",
    "Professional & Business Services",
    "IT & Digital",
    "Health & Social Care",
    "Education & Training",
    "Facilities & Estates",
    "Transport & Logistics",
    "Energy & Utilities",
    "Environmental & Waste",
    "Manufacturing & Supply / Goods",
    "Engineering (non-construction)",
    "Defence & Security",
    "Agriculture, Food & Rural",
    "Arts, Culture & Leisure",
    "Social & Community Services",
    OTHER_SECTOR,
)

#: Construction sub-category — assigned ONLY when Construction is the primary
#: or a secondary sector; null otherwise.
CONSTRUCTION_SUBCATEGORIES: tuple[str, ...] = (
    "New build – residential",
    "New build – commercial/industrial",
    "Civil engineering & infrastructure",
    "Refurbishment & renovation",
    "Fit-out & interiors",
    "Demolition & site clearance",
    "Groundworks & foundations",
    "Roofing & cladding",
    "Structural works",
    "Mechanical & electrical (M&E)",
    "Plumbing & heating",
    "HVAC & building services",
    "Electrical installation",
    "Fire safety & protection",
    "Flooring",
    "Painting & decorating",
    "Glazing & windows",
    "Landscaping & external works",
    "Scaffolding & access",
    "Planned & reactive maintenance",
    "Facilities management",
    "Grounds maintenance",
    "Building cleaning",
    "Architecture & design",
    "Surveying",
    "Engineering consultancy",
    "Project management",
    "Health & safety / CDM",
    "Construction materials & products supply",
    "Plant & equipment hire",
    "Other construction-related",
)


def _normalise(value: str) -> str:
    """Lower-case, fold every dash variant to '-', collapse whitespace.

    Makes value-matching tolerant of the model returning an en-dash as a
    hyphen (or vice-versa) without ever storing a non-canonical string.
    """
    text = (value or "").strip().lower()
    text = re.sub(r"[‐-―−]", "-", text)  # ‐‑‒–—―− → -
    return re.sub(r"\s+", " ", text)


_SECTOR_BY_NORM: dict[str, str] = {_normalise(s): s for s in SECTORS}
_SUBCAT_BY_NORM: dict[str, str] = {
    _normalise(c): c for c in CONSTRUCTION_SUBCATEGORIES
}


def canonical_sector(value: str | None) -> str | None:
    """Return the canonical sector string for `value`, or None if not a sector."""
    if not value:
        return None
    return _SECTOR_BY_NORM.get(_normalise(value))


def canonical_subcategory(value: str | None) -> str | None:
    """Return the canonical construction sub-category, or None if not one."""
    if not value:
        return None
    return _SUBCAT_BY_NORM.get(_normalise(value))


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


SYSTEM_INSTRUCTIONS = f"""You are a UK public-procurement classification engine. \
You read a tender's title, description, and buyer, and assign it to a FIXED \
sector taxonomy. You are deterministic and conservative.

Assign EXACTLY ONE primary_sector — the single best fit — from this list of 16:
{chr(10).join("- " + s for s in SECTORS)}

Assign zero or more secondary_sectors from the SAME 16-value list — only \
genuinely applicable ones (e.g. an IT system for a hospital → primary \
"IT & Digital", secondary "Health & Social Care"). Keep it tight; do not pad. \
Never repeat the primary in the secondaries.

If — and only if — "{CONSTRUCTION_SECTOR}" is the primary OR a secondary sector, \
also assign one or more construction_subcategories from this list:
{chr(10).join("- " + c for c in CONSTRUCTION_SUBCATEGORIES)}
Otherwise construction_subcategories MUST be an empty list.

Rules:
- A tender that is clearly not a real opportunity, or that cannot be classified, \
gets primary_sector "{OTHER_SECTOR}".
- Use ONLY the exact strings from the lists above — do not invent categories.
- Base the decision on the evidence given; do not guess beyond it.

Return ONLY a strict JSON object, no preamble and no markdown fencing, of the form:
{{"primary_sector": string, "secondary_sectors": [string, ...], \
"construction_subcategories": [string, ...]}}"""


def system_prompt_blocks() -> list[dict]:
    """The system prompt as Anthropic content blocks with prompt caching.

    The taxonomy is identical on every call, so the block is marked
    ``cache_control: ephemeral`` — subsequent calls within the cache window
    pay the cheaper cached-input rate for these tokens.
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_INSTRUCTIONS,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _utf8_safe(text: str) -> str:
    """Replace characters that cannot survive UTF-8 encoding (lone surrogates
    from badly-scraped HTML). One poisoned tender must not be able to abort a
    whole batch submission at JSON-serialisation time — the payload of a
    Batch-API create carries hundreds of tenders."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def build_user_text(
    title: str | None, description: str | None, buyer_name: str | None
) -> str:
    """The per-tender user message. Title + description (+ buyer if present)."""
    parts = [f"Title: {title or '(untitled)'}"]
    if buyer_name:
        parts.append(f"Buyer: {buyer_name}")
    desc = (description or "").strip()
    if desc:
        # Bound the description so one giant tender can't blow the token budget.
        parts.append(f"Description: {desc[:4000]}")
    return _utf8_safe("\n".join(parts))
