"""Parse a mandatory-requirement string into a structured probe.

The brief's `mandatory_requirements` is a list of free-text strings copied
verbatim from the ITT — "£10m Professional Indemnity insurance",
"ISO 27001 certified", "minimum £5m turnover", etc. To reconcile against the
vault we need a typed shape.

We deliberately stay close to the brief text: where we can extract a number /
unit / standard reliably with regex, we do; otherwise we return a
`Probe` with `kind="unknown"` so the caller can emit `please_confirm`
rather than fabricate a false negative.

NO LLM call here. The brief engine already does the hard interpretation; we
only pull out the bits a reconciliation needs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ProbeKind = Literal[
    "insurance",         # PI / EL / PL / cyber insurance limit
    "turnover",          # minimum company turnover
    "iso_standard",      # ISO 27001, ISO 9001, ISO 14001, ...
    "accreditation",     # Cyber Essentials (+/- Plus), Constructionline, ...
    "unknown",
]


@dataclass(frozen=True)
class Probe:
    """A parsed view of a single mandatory_requirement string. `original` is
    the verbatim ITT line for error messages."""

    kind: ProbeKind
    original: str
    # Numeric threshold (currency for insurance/turnover; null for cert).
    threshold_value: Decimal | None = None
    currency: str | None = None
    # Insurance subtype if we could tell ("professional_indemnity",
    # "public_liability", etc. — matches the vault InsuranceType enum).
    insurance_type: str | None = None
    # Specific standard or accreditation name we recognised.
    standard: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Capture a money amount with optional currency symbol and an m/k/bn/million
# suffix. Examples matched: "£10m", "£5,000,000", "£10 million", "5m GBP".
_MONEY_RE = re.compile(
    r"""
    £\s*                                     # optional £ symbol
    (?P<num>\d+(?:[\d,]*\d)?(?:\.\d+)?)      # number
    \s*
    (?P<unit>k|m|bn|b|million|thousand|billion)?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_MONEY_NO_SYMBOL_RE = re.compile(
    r"""
    \b
    (?P<num>\d+(?:[\d,]*\d)?(?:\.\d+)?)
    \s*
    (?P<unit>k|m|bn|b|million|thousand|billion)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_INSURANCE_HINTS: dict[str, str] = {
    "professional indemnity": "professional_indemnity",
    "pi insurance": "professional_indemnity",
    "professional liability": "professional_indemnity",
    "public liability": "public_liability",
    "pl insurance": "public_liability",
    "employer's liability": "employers_liability",
    "employers liability": "employers_liability",
    "el insurance": "employers_liability",
    "cyber insurance": "cyber",
    "cyber liability": "cyber",
    "product liability": "product",
}

# ISO standards we'll surface explicitly. Anything else (e.g. ISO 22301) is
# returned with whatever number we saw — vault matching uses the same number.
_ISO_RE = re.compile(r"\bISO\s*(?P<num>\d{4,5}(?::\d{4})?)\b", re.IGNORECASE)

_ACCREDITATION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcyber\s*essentials\s*plus\b", re.IGNORECASE), "cyber_essentials_plus"),
    (re.compile(r"\bcyber\s*essentials\b(?!\s*plus)", re.IGNORECASE), "cyber_essentials"),
    (re.compile(r"\bconstructionline\b", re.IGNORECASE), "constructionline"),
    (re.compile(r"\bsafecontractor\b", re.IGNORECASE), "safecontractor"),
    (re.compile(r"\bchas\b", re.IGNORECASE), "chas"),
]


def _parse_money(text: str) -> tuple[Decimal | None, str | None]:
    """Try to pull a money amount + currency out of `text`. Returns
    (None, None) if no plausible amount is present."""
    match = _MONEY_RE.search(text)
    has_pound = match is not None and "£" in text[max(0, match.start() - 1) : match.end()]
    if match is None:
        match = _MONEY_NO_SYMBOL_RE.search(text)
        if match is None:
            return None, None
    raw_num = match.group("num").replace(",", "")
    try:
        amount = Decimal(raw_num)
    except Exception:  # noqa: BLE001
        return None, None
    unit = (match.group("unit") or "").lower()
    multiplier = {
        "k": 1_000,
        "thousand": 1_000,
        "m": 1_000_000,
        "million": 1_000_000,
        "bn": 1_000_000_000,
        "b": 1_000_000_000,
        "billion": 1_000_000_000,
        "": 1,
    }[unit]
    amount = amount * Decimal(multiplier)
    currency = "GBP" if has_pound or "gbp" in text.lower() else None
    return amount, currency


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_requirement(line: str) -> Probe:
    """Best-effort parse. Order matters: we check the most specific cases
    (insurance, turnover) before falling back to standards / accreditations
    / unknown."""
    text = line.strip()
    if not text:
        return Probe(kind="unknown", original=line)
    lower = text.lower()

    # 1) insurance
    insurance_type: str | None = None
    for hint, value in _INSURANCE_HINTS.items():
        if hint in lower:
            insurance_type = value
            break
    if insurance_type is not None:
        amount, currency = _parse_money(text)
        return Probe(
            kind="insurance",
            original=line,
            threshold_value=amount,
            currency=currency,
            insurance_type=insurance_type,
        )

    # 2) turnover
    if "turnover" in lower or "annual revenue" in lower:
        amount, currency = _parse_money(text)
        return Probe(
            kind="turnover",
            original=line,
            threshold_value=amount,
            currency=currency,
        )

    # 3) ISO standard
    iso_match = _ISO_RE.search(text)
    if iso_match:
        return Probe(
            kind="iso_standard",
            original=line,
            standard=iso_match.group("num"),
        )

    # 4) Accreditations
    for pattern, name in _ACCREDITATION_PATTERNS:
        if pattern.search(text):
            return Probe(kind="accreditation", original=line, standard=name)

    return Probe(kind="unknown", original=line)
