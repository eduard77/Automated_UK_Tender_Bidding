Task: build a dedicated tender search/filter page in the dashboard, backed by a search API over the existing tenders table. Source-agnostic by design (works across all current and future sources), click-to-search, duplicates shown but marked. Autonomous run. Single PR against main.

This is prompt 7 in the Phase 4 build sequence. The tenders table ALREADY has the full common shape and a populated dedup key — this prompt does NOT change the model. It builds the user-facing filter over what exists. There are currently ~3,308 tenders (all source_code = Contracts Finder), procurement_ref populated on 100%, duplicate_of_id used for dedup. The filter must be SOURCE-AGNOSTIC so that when Find a Tender / Proactis / Delta tenders are ingested later, they appear in the same filter with no changes.

Required reading first:
- tender_agent/db/models.py (or models.py) — the Tender model. Columns to filter on: title, description, keywords (ARRAY), cpv_codes (ARRAY), buyer_name, buyer_region, status, source_code, value_amount/value_min/value_max, deadline_at, published_at, procurement_ref, duplicate_of_id, source_url.
- tender_agent/api/ — existing tender endpoints + how list/detail endpoints + pagination are structured (mirror conventions). The existing GET tender detail powers /tenders/[id].
- tender_agent/schemas.py — response schema patterns
- tender-agent-dashboard/ — app router structure, lib/api.ts, the Elevated Genera theme + components, the existing tenders list/detail pages and their styling. The new page must match the theme.

================================================================
PART A — SEARCH API
================================================================
Add a search endpoint: GET /tenders/search (or POST if param count is cleaner) with these OPTIONAL filters, all combinable (AND across fields):
- q: free text — case-insensitive match across title + description (+ keywords). Use ILIKE / trigram-ish matching; keep it simple and indexable.
- cpv: one or more CPV codes — match if any overlaps the tender's cpv_codes array.
- region: buyer_region (one or more; exact or ILIKE).
- buyer: buyer_name ILIKE.
- value_min / value_max: numeric range — match tenders whose value_amount (or value_min..value_max band) falls in/overlaps the requested range. Handle nulls gracefully (a tender with no value shouldn't be wrongly excluded unless a value filter is set — decide and document: if value filter set, exclude null-value tenders).
- deadline_from / deadline_to: deadline_at within the window. Support a convenience like "open only" (deadline_at >= now AND status open).
- status: one or more (open / closed / etc. — use the actual distinct values present).
- source: one or more source_code (so the user CAN narrow to a source, but default is ALL sources).
- include_duplicates: bool, default TRUE (show all, mark duplicates per the product decision). When true, return every match but annotate each result with is_duplicate (duplicate_of_id is not null) and, if duplicate, the primary tender id. When false, return only primaries (duplicate_of_id is null).

Response: paginated (page/page_size, sensible default e.g. 25, max 100), with total count, and each result carrying the fields the list UI needs: id, title, buyer_name, buyer_region, value (amount/currency or min-max), deadline_at, status, source_code, cpv_codes, is_duplicate, duplicate_of_id, source_url. Sort options: deadline_at asc (closing soonest) default, plus published_at desc (newest), value desc. 

Performance: add DB indexes if missing for the common filters (status, deadline_at, source_code, buyer_region; GIN on cpv_codes/keywords arrays; a text index for q). Migration for indexes only — do NOT alter columns.

================================================================
PART B — SEARCH/FILTER PAGE (dashboard)
================================================================
New dedicated page, e.g. /search (link it in the dashboard nav). Elevated Genera theme, matching existing pages.
- A filter panel with: free-text search box (q); CPV/sector input (multi); region (multi, ideally a select populated from distinct buyer_region values via a small lookup endpoint or a static list — keep simple); buyer text; value min/max; deadline from/to (+ an "Open only" quick toggle); status (multi); source (multi, default all); an "include duplicates" toggle (default on, since the decision is show-all-marked).
- A prominent **Search** button — results update ON CLICK, not live. (Per product decision.) Filters can be set freely; nothing queries until Search is pressed. Provide a Clear/Reset.
- Results list: each tender as a card/row showing title (link to /tenders/[id]), buyer, region, value, deadline (with "closing in X days" if open), status, and a small SOURCE badge (Contracts Finder, etc.). Duplicates clearly MARKED — a subtle "duplicate" chip with a tooltip/link to the primary listing (so the user sees the overlap, per decision). 
- Pagination controls; result count ("327 tenders match"); sort dropdown (closing soonest / newest / highest value).
- Empty state ("No tenders match these filters") and loading state.
- Each result links through to the existing tender detail page (where fetch-documents + generate-brief already live), so search → pick → fetch → brief is one continuous flow.

================================================================
PART C — TESTS
================================================================
Backend (real or mocked DB session, no network):
- each filter in isolation (q, cpv overlap, region, buyer, value range incl. null handling, deadline window, status, source) returns correct subset.
- combined filters AND correctly.
- include_duplicates true → all results, is_duplicate annotated; false → primaries only.
- pagination + total count correct; sort orders correct.
- source-agnostic: inserting a tender with a different source_code makes it appear (and be filterable by source) with no code change.
- dedup annotation: a tender with duplicate_of_id set is marked and references its primary.
Dashboard:
- filter page renders; Search triggers the query (and only on click); results render with source badge + duplicate chip; pagination works; clear resets. (Component/integration tests per the dashboard's existing test setup; if none, at least tsc + build clean and a basic render test.)
- Keep ALL existing tests green.

================================================================
VERIFICATION
================================================================
- pytest green; ruff clean; dashboard tsc --noEmit + npm run build clean.
- Manual test note: open /search, filter the existing ~3,308 Contracts Finder tenders by e.g. region + open-only + a keyword → correct results, closing-soonest first, duplicates marked, click through to a tender detail. Confirm source badge shows. Confirm that with no filters, Search returns all (paginated) sorted by closing soonest.

================================================================
SHIP
================================================================
Commits:
- feat(api): tender search endpoint (all filters, dedup-aware, paginated, sorted) + filter indexes migration
- feat(dashboard): dedicated search/filter page (click-to-search, source badges, duplicate marking) + nav link
- test: search filters/combinations/dedup/pagination + page render

PR title: "feat: Phase 4 Chunk 7 — tender search & filter (multi-source, dedup-aware)"
Description: a dedicated, source-agnostic search page over the existing tender model; click-to-search; shows all results with duplicates marked (procurement_ref/duplicate_of_id); built so future sources (Find a Tender, Proactis, Delta) appear automatically. Manual test steps. Sentinel: "Phase 4 Chunk 7 — tender search & filter. Ready for review."

RULES
- Single PR against main, from CURRENT main. Do NOT alter the tenders model/columns (indexes only). Source-agnostic — never hardcode Contracts Finder. Reuse existing API + dashboard conventions + Elevated Genera theme. Keep all existing tests green. If blocked, "BLOCKED:" at top.

Begin.
