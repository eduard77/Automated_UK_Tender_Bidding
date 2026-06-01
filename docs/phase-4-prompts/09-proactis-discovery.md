Task: build Proactis / ProContract (procontract.due-north.com) opportunity DISCOVERY — a background job that logs in via the browser bridge, applies configured filters on the Find Opportunities page, walks the filtered results, opens each opportunity for its detail (crucially the DN reference), and upserts them into the tenders table so they appear in the existing unified search (deduped against Contracts Finder / Find a Tender). Autonomous run. Single PR against main.

This is prompt 9. CONTEXT: the Proactis document-fetch ADAPTER already exists (chunk 6, merged) — given a Proactis tender it fetches documents. But no Proactis tenders enter the system, so the adapter has nothing to act on. This prompt builds the DISCOVERY half that pulls Proactis opportunities INTO the tenders table. CF/FTS discovery is API-based; Proactis has no API, so discovery uses the browser bridge (like the Proactis adapter does). This is the FIRST portal-based discovery — set a clean, reusable pattern future portals (Delta etc.) will follow.

This is STEP 1 of 2. Step 1: discovery driven by a FILTER CONFIG (regions/keywords/categories) that we set. Step 2 (later) wires the main filter page to this config. Do NOT build the filter-page wiring here — just accept a config.

Required reading first:
- tender_agent/services/portals/adapters/proactis.py — the existing Proactis adapter: PROACTIS_URLS/PROACTIS_SELECTORS, login/is_authenticated, how it navigates due-north, reads rendered DOM. REUSE its login + selectors + bridge usage. Discovery and the adapter should share Proactis constants (don't duplicate URLs/selectors — refactor shared ones into a common module if needed).
- how CF/FTS discovery/ingestion works — find the existing ingestion/poll service (the thing that pulls CF tenders into the tenders table, sets procurement_ref, region via the chunk-8 resolver, dedup via duplicate_of_id/procurement_ref, first_seen_at/last_seen_at, content_hash). Mirror its UPSERT + dedup + region-assignment exactly so Proactis tenders are first-class and dedupe against CF/FTS.
- tender_agent/services/bridge_client.py — bridge primitives (navigate, rendered-html, fill, click, select-option, wait-for-login, page-text).
- tender_agent/db/models.py — Tender model (source_code, source_ref, source_url, procurement_ref, title, buyer_name, value_amount, published_at, deadline_at, region, raw, first_seen_at, last_seen_at, content_hash, duplicate_of_id).
- tender_agent/services/regions.py — the resolver (assign region on upsert).
- poll_runs table / any existing scheduled-job pattern — mirror it for scheduling.
- migration numbering (latest 0012 → 0013).

================================================================
CONFIRMED PROACTIS DISCOVERY FLOW (from live screenshots)
================================================================
- Login: procontract.due-north.com/Login/Index — username/password, NO 2FA (human logs in via the visible bridge window; never submit credentials).
- Find Opportunities listing: procontract.due-north.com/Opportunities/Index?tabName=opportunities (paginated — can be ~69 pages unfiltered; with filters far fewer).
- Filters on that page ("Narrow your results"): Portals (dropdown), Organisations (dropdown), Categories (Add UNSPSC/CPV/ProClass/etc.), Regions (Add new region), Keywords (text box), Include closed (Yes/No — we want NO = open only), Expression date range, Published date range. An "Update" button applies filters; "Reset" clears.
- Listing table columns per row: Title (link), Buyer, Expression Start, Expression End, Estimated value. Title links to the opportunity detail page.
- Opportunity detail: procontract.due-north.com/Supplier/Advert/View?advertId={GUID}. Contains: Opportunity Id = the DN REFERENCE (e.g. "DN815596") ← THE DEDUP KEY (same DN format as Contracts Finder/FTS procurement_ref), Title, Categories, Description, Region(s) of supply (e.g. "Merseyside"), Estimated value, Keywords, Buyer, contract start/end dates, Expression of interest window, "Register interest in this opportunity" button.

================================================================
WHAT TO BUILD
================================================================
PART A — discovery filter config
1. A config object/model for Proactis discovery filters: regions (list), keywords (list/string), categories (list), organisations/portals (optional), include_closed (default False = open only). For Step 1, source this config from settings/env or a simple config row (NOT the filter UI — that's Step 2). Make it a clean typed object (pydantic) so Step 2 can populate it from the filter page later. Document where to set it for the manual test.

PART B — the discovery service (bridge-driven)
2. tender_agent/services/discovery/proactis_discovery.py:
   - Open a bridge session, ensure authenticated (reuse the adapter's is_authenticated + human login wait; same single-session care as the adapter).
   - Navigate to Find Opportunities, APPLY the configured filters: set Keywords, add Regions, add Categories, set Include closed = No, click Update. Use the adapter's rendered-DOM reading + the page's controls (select-option/fill/click via the bridge). Robust waits for the filtered listing to render (the page is server-rendered with some JS — poll like the adapter does).
   - WALK the filtered result pages: read each row (Title, Buyer, Expression Start/End, Estimated value, the Title link's advertId). Follow pagination (Next > / page numbers) until done or a sane page cap (config, e.g. max_pages default generous but bounded). Log discovery.page (n, rows).
   - For EACH opportunity: open its detail page (Supplier/Advert/View?advertId=...), read: Opportunity Id (DN reference), Title, Categories, Description, Region(s) of supply, Estimated value, Keywords, Buyer, contract dates, expression window. (Per the agreed decision — open each because the set is small after filtering, and the DN is only on the detail page and is required for dedup.)
   - UPSERT into tenders via the SAME path CF/FTS ingestion uses:
       * source_code = a Proactis code (e.g. "PROACTIS" or the existing slug "procontract" — match whatever convention CF/FTS use for source_code values).
       * procurement_ref = the DN reference (THE dedup key) → existing dedup logic merges with any CF/FTS record sharing that DN (set duplicate_of_id per the existing rule; do NOT bypass it).
       * source_ref = advertId (the GUID); source_url = the Advert/View URL (so the adapter can later locate it / user can open it).
       * title, buyer_name, value_amount (parse "£192,000.00"), published_at if available, deadline_at = expression end, contract_start/contract_end, description, keywords, cpv_codes if categories map to CPV (best-effort), raw = the captured detail.
       * region = via the chunk-8 resolver (Region(s) of supply string → canonical region; "Merseyside" → North West, etc. — feed it through resolve()).
       * first_seen_at/last_seen_at, content_hash (mirror CF ingestion's change-detection so re-runs update last_seen_at and don't duplicate).
   - Idempotent: re-running discovery updates existing rows (by source_ref/procurement_ref), inserts only new, never duplicates.
   - Record a poll_runs-style row for the discovery run (counts: pages walked, opportunities seen, inserted, updated, deduped-against-existing). Log throughout (discovery.proactis.* events).
   - Session release at the end (mirror the adapter).

PART C — scheduling (background, not live)
3. Wire Proactis discovery to run as a BACKGROUND job on the existing schedule mechanism (the poll_runs / scheduler pattern CF uses). It must NOT run on the search request path — it's a scheduled/triggerable background job. Also expose a manual trigger (an API endpoint or a runnable script, e.g. python -m ... or POST /discovery/proactis/run) so the user can kick it off on demand for the test. Because it needs a human bridge login, the job: if not authenticated, surfaces a "needs login" state (reuse the bridge wait-for-login pattern) rather than failing silently.

================================================================
TESTS (mocked bridge, no network, no real Proactis)
================================================================
- filter application: given a config, asserts the discovery sets keywords/regions/categories + include_closed=No and clicks Update (mock the bridge calls).
- listing walk: mocked paginated listing → reads all rows across pages, respects max_pages cap.
- detail read: mocked detail page → extracts DN reference, region, value, dates correctly; value/date parsing ("£192,000.00", dd/mm/yyyy) correct.
- upsert + dedup: a discovered opportunity whose DN matches an existing CF tender → deduped (duplicate_of_id set), not a second primary; a new DN → inserted as primary with region resolved; re-run → updates last_seen_at, no duplicate. ASSERT it uses the SAME ingestion/upsert + region resolver as CF (not a parallel path).
- region resolution: "Merseyside" → North West (via resolver); a region string already canonical passes through.
- idempotency + run record counts.
- needs-login path surfaces cleanly.
- All existing tests green (adapter, search, regions, brief).

================================================================
VERIFICATION
================================================================
- pytest green; ruff clean; bridge imports clean; dashboard unaffected (no UI in Step 1).
- No real Proactis login in the run. Manual test (desktop, this afternoon): set the discovery config (document how), start the bridge, log into Proactis at the bridge window, trigger discovery → it applies filters, walks the filtered opportunities, upserts them with DN refs → new Proactis tenders appear in the tenders table (source_code Proactis), deduped against any CF copies, with regions assigned → they show up in the existing /search filter alongside CF/FTS. Then a discovered Proactis tender can be opened and its documents fetched via the existing Proactis adapter (closing the loop).

================================================================
SHIP
================================================================
Commits:
- feat(discovery): Proactis discovery config (filters: regions/keywords/categories, open-only)
- feat(discovery): bridge-driven Proactis opportunity discovery — filter, paginate, read detail (DN ref), upsert via shared ingestion + dedup + region resolver
- feat(discovery): background scheduling + manual trigger; needs-login handling; run record
- test: filter apply / listing walk / detail+DN parse / upsert+dedup / region / idempotency / needs-login

PR title: "feat: Phase 4 Chunk 9 — Proactis opportunity discovery (background, filter-driven, dedup via DN)"
Description: explain this closes the Proactis loop (the adapter existed but no Proactis tenders were entering the system); bridge-driven discovery applies configured filters, walks filtered results, opens each for the DN reference (the CF-compatible dedup key), upserts via the SAME ingestion+dedup+region path as CF/FTS so Proactis tenders appear in unified search deduped; runs in the BACKGROUND on schedule (not live on search) with a manual trigger for testing; this is Step 1 (config-driven) — Step 2 will wire the main filter page to the config. Manual test steps. Sentinel: "Phase 4 Chunk 9 — Proactis discovery. Ready for review."

RULES
- Single PR against main, from CURRENT main. REUSE the Proactis adapter's login/selectors and the CF ingestion's upsert+dedup+region resolver — do NOT fork them. Never submit credentials. Discovery is BACKGROUND/triggerable, never on the search request path. Open opportunities only (include_closed default False). Bridge single-session care + session release. Mocked tests only. If blocked, "BLOCKED:" at top.

Begin.
