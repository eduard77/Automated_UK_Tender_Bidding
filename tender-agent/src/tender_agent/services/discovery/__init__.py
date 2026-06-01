"""Background opportunity discovery (Phase 4 chunk 9+).

Each tender source that needs **browser-driven** discovery (i.e. a portal
without a public API — Proactis today, Delta etc. later) lives here. Pure-HTTP
sources stay in `tender_agent.adapters.*` and feed through `services/ingestion`.

Convention: one module per portal, named `<portal>_discovery.py`. Each
exposes a `run(...)` coroutine that:
  - obtains a bridge session (single-session, reused with the document-fetch
    adapter to avoid stealing the operator's login),
  - applies a typed `*FilterConfig` (regions / keywords / categories / etc.),
  - walks the filtered listing,
  - opens each opportunity for its DN reference (the cross-source dedup key),
  - upserts each via the SAME path CF/FTS use
    (`services.ingestion._upsert_tender` + `services.deduplicator.find_duplicate`
    + `services.regions.resolve_for_tender`) so cross-source dedup by
    `procurement_ref` works.

Discovery NEVER runs on the search request path — it's a scheduled background
job plus an admin manual trigger.
"""
from __future__ import annotations
