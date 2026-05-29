#!/usr/bin/env python3
"""Backfill the canonical ``region`` column for every tender that lacks one.

The standard UK region is almost always ALREADY present in the raw OCDS
delivery-address data (``region`` field -> ``postalCode`` -> ``countryName``) —
we just weren't extracting it. This script runs the same resolver the live
ingestion pipeline uses (``services.regions``) over every NULL-region tender:

    a. OCDS region (exact name / NUTS code)  -> method ``ocds_region``
    b. UK postcode area                       -> method ``postcode``
    c. country name                           -> method ``country``
    d. AI fallback (rare, cached)             -> method ``ai``
    e. nothing usable                         -> ``Unspecified``

Because (a)-(c) are exact/deterministic, the AI fallback is expected to be a
tiny fraction. Each distinct unresolved key is resolved once and cached in the
``region_lookup`` table, so re-runs never re-ask the LLM. Only NULL-region rows
are touched, so the script is safe to re-run after new tenders arrive.

Usage:
    python scripts/normalise_regions.py                 # deterministic + AI fallback
    python scripts/normalise_regions.py --no-ai         # deterministic only
    python scripts/normalise_regions.py --batch-size 1000

The AI fallback reuses the existing brief LLM client and needs ANTHROPIC_API_KEY.
If it is missing, the script proceeds deterministically (AI disabled) and says so.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tender_agent.db import SessionLocal  # noqa: E402
from tender_agent.services.regions import METHODS, normalise_all_tenders  # noqa: E402

# Rough cost of one AI fallback call: a tiny classification prompt (~200 input
# tokens, <=24 output) on the brief model. Indicative only — printed so an
# operator can sanity-check the (expected tiny) spend.
_AI_INPUT_TOKENS = 200
_AI_OUTPUT_TOKENS = 24
_AI_INPUT_PER_MTOK_USD = 3.0  # Sonnet-class input
_AI_OUTPUT_PER_MTOK_USD = 15.0  # Sonnet-class output


def _build_llm(no_ai: bool) -> object | None:
    if no_ai:
        print("AI fallback disabled (--no-ai): deterministic resolution only.")
        return None
    # Resolve the API key the same way the brief client does.
    from tender_agent.services.brief.llm_client import (
        AnthropicBriefLLMClient,
        _resolve_api_key,
    )

    if not _resolve_api_key():
        print(
            "ANTHROPIC_API_KEY not set — AI fallback disabled, resolving "
            "deterministically. Unresolved free-text locations become "
            "'Unspecified' (re-run with the key set to upgrade them)."
        )
        return None
    return AnthropicBriefLLMClient()


def _estimate_ai_cost(ai_calls: int) -> float:
    in_cost = ai_calls * _AI_INPUT_TOKENS / 1_000_000 * _AI_INPUT_PER_MTOK_USD
    out_cost = ai_calls * _AI_OUTPUT_TOKENS / 1_000_000 * _AI_OUTPUT_PER_MTOK_USD
    return in_cost + out_cost


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Resolve deterministically only; never call the LLM.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Tenders processed and committed per batch (default 500).",
    )
    args = parser.parse_args()

    llm = _build_llm(args.no_ai)

    with SessionLocal() as db:
        counts = normalise_all_tenders(db, llm=llm, batch_size=args.batch_size)

    total = sum(counts.values())
    print("\nDone. Region assignment by method:")
    for method in METHODS:
        n = counts.get(method, 0)
        pct = (100.0 * n / total) if total else 0.0
        print(f"  {method:<12} {n:>8}  ({pct:5.1f}%)")
    print(f"  {'TOTAL':<12} {total:>8}")

    ai_calls = counts.get("ai", 0)
    if ai_calls:
        cost = _estimate_ai_cost(ai_calls)
        print(
            f"\nAI fallback was used for {ai_calls} distinct unresolved "
            f"location key(s). Estimated cost ~${cost:.4f} "
            f"(~{_AI_INPUT_TOKENS} in / {_AI_OUTPUT_TOKENS} out tokens each; "
            "indicative only)."
        )
    else:
        print("\nAI fallback was not needed — every region resolved deterministically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
