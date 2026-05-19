Task: build the portal discovery + registry foundation for Phase 4. Autonomous
end-to-end run, no human in the loop. The user is asleep / unavailable.

This is prompt 1 of 12 in the Phase 4 build sequence. See 
docs/phase-4-design.md for the full architecture. This prompt must produce a
clean, merged PR with the foundation everything else depends on.

================================================================
CONTEXT YOU NEED
================================================================

- Repo: C:\Code\PublicTender (Windows host) — but you're running in WSL/Linux
  via Docker.
- Backend lives in `tender-agent/` (Python 3.12, FastAPI, SQLAlchemy 2, 
  Alembic, Postgres 16 with pgvector).
- Dashboard lives in `tender-agent-dashboard/` (Next.js 15, App Router, 
  Tailwind 4, Elevated Genera theme — PR #22 merged).
- Existing components available: TenderCard, PillKicker, StatBlock, 
  FilterChip, AppShell, AtmosphereBackground, VaultDocumentCard, 
  ClaimsDisplay, VaultUploadModal.
- Current data state: ~3000+ tenders ingested from CF/FTS/PCS adapters into
  `tenders` table.
- Latest migration on main: 0004_vault.py
- Design doc to read first: docs/phase-4-design.md (specifically sections 
  "1. Portal discovery" and "2. Portal registry")
- Core principle from design doc: machines act, humans interpret. Mechanical 
  work autonomous; interpretive moments pause for user. Apply throughout.

================================================================
PART A — BACKEND: SCHEMA
================================================================

Branch: feat-portal-discovery

1. Create migration `0005_portal_registry.py`:

   Table `portals`:
   - id (bigserial PK)
   - domain (text, unique, not null) — lowercase, no scheme, no path. 
     E.g. "procontract.due-north.com"
   - display_name (text, not null) — defaults to domain when unknown, 
     human-friendly when classified
   - url_patterns (jsonb, default '[]') — array of regex strings identifying
     tender pages on this domain
   - login_type (text, not null, default 'unknown') — one of: none, 
     email_only, username_password, 2fa, oauth, unknown
   - adapter_status (text, not null, default 'not_started') — one of: 
     not_started, stub, read_only, full, deprecated
   - adapter_module (text, nullable) — Python module path once an adapter 
     exists, e.g. 'tender_agent.adapters.portals.procontract'
   - priority (text, not null, default 'medium') — one of: critical, high, 
     medium, low
   - tender_count (integer, not null, default 0) — running count of distinct 
     tenders we've seen link here
   - first_seen_at (timestamp, not null, default now())
   - last_seen_at (timestamp, not null, default now())
   - classification_data (jsonb, nullable) — Claude's classification result
   - notes (text, nullable) — free-text human notes
   - created_at (timestamp, default now())
   - updated_at (timestamp, default now())
   
   Indexes:
   - UNIQUE INDEX on (domain)
   - INDEX on (adapter_status)
   - INDEX on (priority)
   - INDEX on (last_seen_at DESC)

   Table `portal_url_sightings`:
   - id (bigserial PK)
   - portal_id (bigint, FK to portals.id, ON DELETE CASCADE, nullable until
     classified)
   - tender_id (bigint, FK to tenders.id, ON DELETE CASCADE, not null)
   - url (text, not null) — full URL as extracted, before normalisation
   - extracted_from (text, not null) — one of: description, 
     additional_information, documents, contact, parties
   - sighting_type (text, not null) — one of: tender_link, document_link, 
     contact_email, reference_text
   - extracted_at (timestamp, not null, default now())
   
   Indexes:
   - INDEX on (portal_id)
   - INDEX on (tender_id)
   - COMPOSITE INDEX on (portal_id, extracted_at DESC)

   Table `portal_blocklist_domains`:
   - id (bigserial PK)
   - domain (text, unique, not null)
   - reason (text, nullable) — human-readable why blocked
   - added_at (timestamp, not null, default now())
   - added_by (text, not null, default 'system') — 'system' for seeded, 
     'user' for dashboard-added

   Indexes:
   - UNIQUE INDEX on (domain)

   Seed blocklist with these domains at migration time (added_by='system'):
   - contractsfinder.service.gov.uk
   - find-tender.service.gov.uk
   - publiccontractsscotland.gov.uk
   - sell2wales.gov.wales
   - etendersni.gov.uk
   - twitter.com
   - x.com
   - linkedin.com
   - facebook.com
   - youtube.com
   - google.com
   - microsoft.com
   - office.com
   - bing.com
   - gov.uk (root, but allow subdomains — see extraction rules below)

2. SQLAlchemy ORM models in `tender_agent/db/models.py`:
   - Portal (matches portals table)
   - PortalUrlSighting (matches portal_url_sightings table)
   - PortalBlocklistDomain (matches portal_blocklist_domains table)
   
   Use SQLAlchemy 2.0 `Mapped` syntax. Relationships:
   - Portal.sightings → list[PortalUrlSighting]
   - PortalUrlSighting.portal → Portal
   - PortalUrlSighting.tender → Tender

3. Pydantic schemas in `tender_agent/api/schemas/portals.py`:
   - PortalRead (full read model)
   - PortalUpdate (admin override — display_name, login_type, priority, 
     adapter_status, notes)
   - PortalUrlSightingRead
   - PortalBlocklistEntryRead, PortalBlocklistEntryCreate
   - Enums: LoginType, AdapterStatus, Priority, ExtractedFrom, SightingType

================================================================
PART B — BACKEND: URL EXTRACTION PIPELINE
================================================================

4. Service: `tender_agent/services/portal_discovery.py`
   
   Function: `extract_urls_from_tender(tender, db) -> list[ExtractedUrl]`
   
   Inputs: a Tender ORM instance, db session.
   
   Process:
   a. Collect candidate URLs from these fields on the tender:
      - tender.additional_information (string field) — regex extract URLs
      - tender.description — regex extract URLs AND email addresses
      - tender.documents (existing JSONB array on tender) — extract each 
        document URL
      - If tender source data has parties[].contactPoint.url or .email, 
        extract those too (look at raw_payload jsonb)
   
   b. For each candidate URL:
      - Normalise: lowercase scheme + host, strip query strings (BUT preserve
        query strings that contain "tender" or "id" or "ref" in the key — 
        these often hold the tender identifier)
      - Strip fragments
      - Extract bare domain for portal grouping
   
   c. Filter out blocklist matches. A URL is filtered if its bare domain 
      matches any portal_blocklist_domains entry.
      Special case: gov.uk is blocklisted as root, but specific subdomains 
      that look like procurement portals (e.g. nepo.gov.uk, lhc-pg.co.uk are
      kept). Rule: if domain ends in ".gov.uk" AND is exactly two labels 
      ("gov.uk"), block. Otherwise allow.
   
   d. Return list of ExtractedUrl dataclasses: 
      (url, normalised_url, domain, extracted_from, sighting_type)
   
   Function: `process_tender_for_portals(tender, db) -> ProcessResult`
   
   Inputs: a Tender ORM instance, db session.
   
   Process:
   1. Call extract_urls_from_tender
   2. For each ExtractedUrl:
      a. Find existing Portal by domain, OR create new one (display_name 
         defaults to domain, all enums default per schema)
      b. Create PortalUrlSighting linking portal + tender + URL
      c. Increment portal.tender_count IF this is a new tender-portal 
         pair (use a check: SELECT existence of any prior sighting for 
         (portal_id, tender_id))
      d. Update portal.last_seen_at = now()
      e. If portal was just created (new this call), queue async 
         classification (see Part C)
   3. Return ProcessResult with counts: 
      (new_portals, new_sightings, classifications_queued)

5. Wire into existing ingestion pipeline:
   
   Find where tenders are inserted/updated after polling (look in 
   tender_agent/services/ingest.py or similar). After each tender is 
   persisted (whether new or updated), call process_tender_for_portals.
   
   Run it inside the same DB transaction as the tender upsert — if portal
   processing fails, log the error but DON'T roll back the tender. Use 
   try/except around the call, log to standard logger at WARNING level.

6. Backfill script: `tender-agent/scripts/backfill_portal_discovery.py`
   
   - Iterates every existing tender in the database (use proper batched 
     iteration — limit/offset, NOT server-side cursor; that bug bit us 
     before)
   - Batches of 200 tenders at a time
   - Calls process_tender_for_portals for each
   - Prints progress: "Processed N/M tenders, found K portals so far"
   - Safe to re-run (idempotent: skip if sighting already exists for 
     (portal_id, tender_id, url))
   - Refuses to run if env TENDER_AGENT_ENV=production (safety guard)
   - At end, prints summary table: top 20 portals by tender_count

   Also create `tender-agent/scripts/README.md` entry for this script if 
   the README exists.

================================================================
PART C — BACKEND: CLASSIFICATION
================================================================

7. Service: `tender_agent/services/portal_classifier.py`
   
   Function: `classify_portal(portal_id, db) -> ClassificationResult`
   
   Process:
   1. Load Portal record
   2. Pick a sample tender URL from its sightings (most recent 
      tender_link or document_link)
   3. HTTP GET the portal homepage (the bare domain). 10s timeout. Catch 
      all errors; if fetch fails, store error in classification_data and 
      return ClassificationResult.status='fetch_failed'.
   4. Truncate HTML body to ~5000 chars (keep <title>, meta tags, and 
      first chunk of body).
   5. Send to Claude (Sonnet — use claude-sonnet-4-6 model name) with a 
      prompt asking for structured JSON output:
      
      {
        "is_procurement_portal": boolean,
        "confidence": float (0.0 to 1.0),
        "suggested_display_name": string,
        "login_type": "none" | "email_only" | "username_password" | "2fa" 
                    | "oauth" | "unknown",
        "suggested_priority": "critical" | "high" | "medium" | "low",
        "reasoning": string (2-3 sentences why),
        "notes": string (anything notable, e.g. "Appears to be a buyer's 
                         own portal" or "Aggregator hosting many buyers")
      }
   
   6. Parse response. If parsing fails or required fields missing, log 
      and store partial result.
   7. Update Portal record:
      - classification_data = full JSON response from Claude
      - display_name = suggested_display_name (only if confidence > 0.7)
      - login_type = suggested login_type (only if confidence > 0.7)
      - priority = suggested_priority (only if confidence > 0.7)
   8. Return ClassificationResult(status='classified', confidence=X)
   
   Async execution: use FastAPI BackgroundTasks. Don't block the 
   ingestion pipeline. Queue classification as a fire-and-forget task. 
   If multiple portals need classification simultaneously, rate-limit to 
   one classification per 3 seconds (don't hammer Claude's API).

8. Test fixture: when running tests, classify_portal should be 
   monkey-patched to return a deterministic mock response instead of 
   calling Claude. No real Claude calls in CI.

================================================================
PART D — BACKEND: API ENDPOINTS
================================================================

9. New router: `tender_agent/api/portals.py`

   Endpoints:
   - GET    /portals — list, with filters: ?adapter_status=, ?priority=, 
     ?search= (LIKE on display_name OR domain), ?has_login_type=. 
     Default sort: priority desc, tender_count desc. Paginated 
     (limit/offset, max limit 100).
   - GET    /portals/{id} — single portal with full classification_data 
     and recent sightings (last 20).
   - PATCH  /portals/{id} — update display_name, login_type, priority, 
     adapter_status, adapter_module, notes. User-driven override of 
     classifier.
   - DELETE /portals/{id} — soft delete via setting 
     adapter_status='deprecated'. Never hard-delete (audit trail).
   - POST   /portals/{id}/classify — manually trigger reclassification 
     for a portal (useful after homepage redesign or initial 
     classification failed).
   - GET    /portals/{id}/sightings — list of sightings for a portal, 
     newest first, paginated.
   
   Blocklist endpoints:
   - GET    /portal-blocklist — list all blocklist entries
   - POST   /portal-blocklist — add a domain. Body: { domain, reason }.
   - DELETE /portal-blocklist/{id} — remove a domain. Existing portals 
     for that domain are NOT auto-removed; they stay in the registry but 
     no new sightings will be created.

10. Mount the router in main.py / app initialization.

11. Tests in tender-agent/tests/:
    - test_portal_discovery_extraction.py: URL extraction edge cases 
      (https vs http, urls in markdown, gov.uk subdomain handling, 
      emails, malformed URLs, etc.)
    - test_portal_discovery_pipeline.py: end-to-end with mock tender, 
      verify portal created, sighting created, blocklist filters work
    - test_portal_classifier.py: mocked Claude response, verify portal 
      record updated correctly, low-confidence responses don't overwrite 
      defaults
    - test_portal_api.py: API endpoints with httpx TestClient
    
    Target 12-15 new tests. Mock Claude calls everywhere. Use an 
    in-memory SQLite or pytest fixture for the database — DON'T require 
    a running Postgres for tests.

================================================================
PART E — DASHBOARD: /PORTALS PAGE
================================================================

12. New route: `tender-agent-dashboard/app/portals/page.tsx`
    
    Page header: PillKicker "Portal registry · live discovery", Fraunces 
    headline "Where UK tenders live.", subhead muted: 
    "All procurement portals discovered from your matched tenders. 
     Classified, prioritised, and ready to integrate."
    
    Stats row (StatBlock component, four blocks):
    - Total portals
    - High/critical priority
    - Adapters built (adapter_status in 'read_only', 'full')
    - Awaiting classification (adapter_status='not_started' AND 
      classification_data IS NULL)
    
    Filter bar (FilterChip pattern from existing code):
    - Status filter: all / not_started / stub / read_only / full / deprecated
    - Priority filter: all / critical / high / medium / low
    - Search input: searches display_name + domain
    - Sort dropdown: by tender_count desc / by priority / by 
      last_seen_at desc / by first_seen_at asc
    
    Main grid: portal cards in two-column layout (collapses to one column 
    under 1100px). Each card uses the Elevated Genera card pattern:
    - Source pill at top: domain in JetBrains Mono
    - Display name in Fraunces 22px
    - Beneath: tender_count badge in mint, priority badge color-coded 
      (critical=red, high=amber, medium=mint, low=dim)
    - Login type as a small chip
    - Adapter status as a status pill (not_started=dim, stub=amber, 
      read_only=mint, full=mint solid, deprecated=red)
    - Bottom strip: "first seen", "last seen" (relative timestamps)
    - On hover: subtle lift, mint border
    - Click: navigates to /portals/{id}

13. Route: `app/portals/[id]/page.tsx` — portal detail page.
    
    Hero: domain in JetBrains Mono pill, big Fraunces display name. 
    Subhead with tender_count, first_seen, last_seen.
    
    Two-column layout below:
    LEFT (2/3 width): Classification panel showing classification_data 
    pretty-printed (is_procurement_portal, confidence as a bar, 
    reasoning, notes). "Re-classify" button at the bottom (POSTs to 
    /portals/{id}/classify).
    
    RIGHT (1/3 width): Admin controls in a card:
    - Display name (editable text input)
    - Login type (select dropdown)
    - Priority (select dropdown with color coded options)
    - Adapter status (select dropdown)
    - Adapter module (text input, mostly read-only — populated when an 
      adapter is built)
    - Notes (textarea)
    - "Save changes" button (PATCH)
    - "Mark deprecated" button (lower right, red, confirmation modal: 
      "This portal will be marked deprecated and no new sightings will 
      count toward it. Existing sightings preserved.")
    
    Below: Recent sightings table (last 20). Columns: tender title 
    (linked to tender detail page), extracted_from, sighting_type, when. 
    "View all sightings" link at the bottom that goes to a paginated 
    list.

14. Route: `app/portals/blocklist/page.tsx` — blocklist management.
    
    Simple list of blocked domains with reason. "Add domain" button 
    opens an inline form (domain input + reason text input + submit). 
    "Remove" button on each row (with confirmation).

15. Add `/portals` to AppShell nav. Active when route starts with 
    /portals.

16. New components:
    - `PortalCard.tsx` — card for the list view
    - `PortalStatusBadge.tsx` — color-coded status display, reusable
    - `PortalPriorityBadge.tsx` — color-coded priority
    - `ClassificationDisplay.tsx` — pretty rendering of classification_data 
      jsonb, with confidence bar component
    - `BlocklistRow.tsx` — row in the blocklist page

17. API client functions in `lib/api.ts`:
    - listPortals(filters)
    - getPortal(id)
    - updatePortal(id, body)
    - reclassifyPortal(id)
    - listSightings(portalId, page)
    - listBlocklist()
    - addBlocklistDomain(body)
    - removeBlocklistDomain(id)

================================================================
PART F — VERIFICATION
================================================================

18. Run the backfill script against the live database:
    
    docker compose exec app python scripts/backfill_portal_discovery.py
    
    Capture the output (total tenders processed, total portals discovered,
    top 20 portals by tender_count). Save this to 
    docs/screenshots/portal-discovery-backfill.txt for the PR.

19. Smoke test the dashboard:
    a. npm run dev (in tender-agent-dashboard)
    b. Navigate to localhost:3000/portals
    c. Verify the list renders, stats row populated, at least 10 portals 
       visible
    d. Filter by adapter_status=not_started — verify filter works
    e. Click into the top-priority portal — detail page renders, 
       classification_data shown, recent sightings populated
    f. Edit display_name, save, verify PATCH succeeded and value 
       persisted on reload
    g. Open /portals/blocklist — verify seed blocklist visible (15 
       entries)
    h. Add a test blocklist domain, verify it appears, remove it, verify 
       it's gone
    i. Resize to 380px — all pages responsive
    
    Capture screenshots:
    - /portals list view with 10+ portals visible
    - /portals/[id] detail view of a high-priority portal
    - /portals/blocklist
    
    Save to docs/screenshots/.

20. Verification commands all clean:
    - cd tender-agent && pytest -v (existing + 12-15 new tests pass)
    - cd tender-agent && ruff check src/ tests/
    - cd tender-agent-dashboard && npx tsc --noEmit (clean)
    - cd tender-agent-dashboard && npm run build (clean, no warnings)

================================================================
PART G — SHIP
================================================================

21. Commit chunks (in order):
    - feat(db): portal registry tables + blocklist with seed
    - feat(services): URL extraction pipeline + ingestion wiring
    - feat(services): Claude-powered portal classifier
    - feat(api): portals + blocklist endpoints
    - feat(scripts): backfill_portal_discovery + README entry
    - feat(dashboard): /portals list + detail + blocklist + components
    - test: portal discovery + classifier + api tests
    - docs: backfill output + screenshots

22. Push branch `feat-portal-discovery`. Open PR.

    Title: "feat: Phase 4 Chunk 1 — Portal discovery + registry"
    
    Description must include:
    - Summary of what's in (per area)
    - Note that this is prompt 1 of 12 in Phase 4 build sequence; design 
      doc at docs/phase-4-design.md
    - Backfill results: total tenders processed, total portals 
      discovered, top 20 portals table embedded
    - 3 dashboard screenshots embedded
    - Smoke test results: which actions you took, all pass
    - Cost note: classification is ~$0.005 per new portal × ~30-50 
      portals expected on first run = ~$0.15-0.25 first-run cost. 
      Acceptable.
    - Anything you noticed during smoke testing that's a follow-up (not 
      a blocker)
    - The sentinel line at the end: "Phase 4 Chunk 1 — Discovery + 
      Registry complete. Ready for review."

    Mark ready for review when CI is green.

================================================================
RULES YOU MUST FOLLOW
================================================================

- Single PR. No intermediate PRs.
- Use the existing Elevated Genera theme. No new visual idioms.
- No backend regressions. All existing endpoints + tests still pass.
- Don't change unrelated code. Stay focused on portal registry work.
- All Claude API calls must respect timeouts. If Claude is unreachable, 
  classification gracefully degrades (portal stored with 
  classification_data='{"status": "claude_unavailable"}', returns 
  normally).
- No live API tests in CI. All tests use mocked Claude responses.
- Backfill script MUST use batched iteration (limit/offset), not 
  server-side cursors. The cursor bug from the previous prompt session 
  must not recur.
- Do NOT stop and ask the user. They are asleep. Document blockers at 
  top of PR with "BLOCKED:" if you absolutely can't proceed.
- Use the GitHub MCP for branch operations if local git proxy is flaky.
- Costs: total run should cost ~$0.50-2 in Claude API calls 
  (classifications + your own internal LLM calls if any). If you see 
  costs spiraling, stop and document.

================================================================
STOP CRITERIA
================================================================

You may only stop when ALL of these are true:
- PR is open against main, marked ready for review
- CI checks green
- pytest -v passes (existing + new)
- npx tsc --noEmit and npm run build are both clean
- ruff check is clean
- 3 dashboard screenshots embedded in PR description
- Backfill output captured and included in PR description
- The sentinel line is present in the PR description: 
  "Phase 4 Chunk 1 — Discovery + Registry complete. Ready for review."

If any blocker is unresolvable, write "BLOCKED:" at the top of the PR 
description with a clear explanation, complete what you can, push the 
branch, and open a draft PR with the BLOCKED note prominent.

Begin.
