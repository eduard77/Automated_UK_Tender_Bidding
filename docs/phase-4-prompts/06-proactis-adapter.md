Task: build a Proactis / ProContract (due-north.com) portal adapter that fetches a tender's documents, following the proven Delta pattern. Reuse all existing infrastructure (browser bridge, persistence, content store). Autonomous run. Single PR against main.

This is prompt 6 in the Phase 4 build sequence. The Delta adapter (browser-bridge based) is complete and proven end-to-end on main: login → express/register interest (with a human-judgment pause) → locate activity → fetch documents → persist to tender_document_files → extract content into tender_document_content. The brief engine reads that content. Build Proactis as a SECOND adapter reusing that exact blueprint and ALL the same infrastructure.

Required reading first:
- tender_agent/services/portals/adapters/delta_esourcing.py — the FULL reference adapter (DELTA_URLS, DELTA_SELECTORS, locate_tender, the already-registered detection, the needs_user_confirmation pause, download_documents, click_download_in_row usage, session release). Mirror its structure for Proactis.
- tender_agent/services/portals/base.py — the adapter interface every adapter implements
- tender_agent/services/portals/registry.py — how adapters are registered/selected per platform
- tender_agent/services/portal_orchestrator.py — the shared flow: login wait, the confirmation pause/resume, _persist_documents, the content-extraction hook (ensure_content_extracted). The Proactis adapter must plug into this SAME orchestration; do NOT fork it.
- tender_agent/services/bridge_client.py — bridge primitives: navigate, page-text, rendered-html, element-exists, fill, click, click_download_in_row, wait-for-login, session open/release
- browser-bridge/bridge/server.py — bridge endpoints (in case a direct-link download helper is needed; see DOWNLOAD STRATEGY)

================================================================
PROACTIS / PROCONTRACT — CONFIRMED FLOW (from live inspection)
================================================================
Platform: Proactis Sourcing, hosted at procontract.due-north.com (branded "Proactis"). NO 2FA — username + password only (human logs in via the visible bridge window, same as Delta; the adapter NEVER submits credentials).

Login URL: https://procontract.due-north.com/Login/Index
Post-login home: https://procontract.due-north.com/SupplierPostLoginHome
Activity (the tender's working page, where documents live): https://procontract.due-north.com/RfxResponse?rfxId={RFX_ID}
The supplier finds opportunities via "Find opportunities" / "My activities"; an opportunity the supplier has expressed interest in becomes an "activity" listed under My activities, reachable as an RfxResponse page.

Interest flow (TWO cases — adapter must handle BOTH):
- The supplier must EXPRESS INTEREST in an opportunity (analogous to Delta's Register Interest; this signals intent to bid → the human-judgment pause applies here).
- After expressing interest, SOME buyers release documents IMMEDIATELY (the activity + documents appear right away); OTHERS require EMAIL CONFIRMATION before the activity/documents become available (the supplier gets an email, then the activity appears later).
  * IMMEDIATE case → proceed to fetch documents.
  * EMAIL-CONFIRMATION case → the adapter CANNOT wait for an out-of-band email. It must PAUSE with a clear status (a distinct terminal/paused state, e.g. status "awaiting_buyer_release" or reuse needs_user_confirmation semantics with a specific detail): "Interest expressed. This buyer requires email confirmation before releasing documents. You'll receive an email shortly; once the activity appears in your account, resume to fetch the documents." When the supplier resumes later (activity now live), the adapter locates the activity and fetches. Mirror Delta's pause/resume machinery; do not invent a parallel mechanism.
- ALREADY-EXPRESSED-INTEREST case (like the test tender): the activity already exists → skip the express-interest step and the pause, go straight to the activity page and fetch. (Mirror Delta's already-registered "no pause" behaviour.)

Activity page document structure (CONFIRMED from live screenshots — simpler than Delta, plain links not ⋮ menus):
- Section "Activity documentation, files & links (N)" — a table with columns Title / Type / Size; each row's Title is a clickable link to the document (e.g. "Section 1 Exec Summary and Specification v6.docx", docx, 127 KB).
- Section "Terms & conditions (N)" — a SEPARATE list with its own document link(s) (e.g. "Services (without TUPE)"). The adapter MUST collect documents from BOTH sections.
- Activity metadata also on the page (useful, capture if easy): Buyer, Title, ID, Description, Deadline ("A response ... no later than {date}").

================================================================
DOWNLOAD STRATEGY (handle either link type)
================================================================
The document links appear to be plain anchors. Implement download with a two-step fallback:
1. PRIMARY — direct link: read each document row's <a href>. If it's a direct file URL, download it via the bridge in the authenticated session (fetch the URL within the logged-in browser context so cookies/session apply, capturing the response as the file — add a small bridge endpoint if needed, e.g. /session/{slug}/download-url that navigates/fetches the href in-session and captures the download, OR reuse expect_download by clicking the link).
2. FALLBACK — click-download: if there's no usable href (script-triggered), reuse the existing click_download_in_row-style click+expect_download against the link element.
Either way, capture each file, name from the link text / Title column, get the extension from the filename or the Type column. Apply the SAME chunk-3 caps + sha256 dedup as Delta. Files flow into the SAME _persist_documents → tender_document_files, then ensure_content_extracted → tender_document_content. Do NOT write a Proactis-specific persistence or content path — route through the shared ones so the dashboard and brief engine work unchanged.

================================================================
ADAPTER IMPLEMENTATION
================================================================
Create tender_agent/services/portals/adapters/proactis.py mirroring delta_esourcing.py:
- PROACTIS_URLS (login, post-login home, find-opportunities, activity RfxResponse pattern) and PROACTIS_SELECTORS (logged-in marker for is_authenticated e.g. the left-nav "Find opportunities"/"My activities" or "Supplier Post-Login Home"; the activity documentation table + rows + link; the terms & conditions list + link; the express-interest control on an opportunity; the "expression of interest successful" confirmation text; the email-confirmation indicator text).
- Implement the same interface methods Delta implements (registry-registered for platform slug "proactis" / "procontract"; check how platforms map to adapters in registry.py and add the mapping + any portal_platforms row/seed needed).
- is_authenticated via a rendered logged-in marker (robust wait, like Delta's 4h fix).
- locate_tender: given the tender, find its activity. If the tender record carries a Proactis rfxId / opportunity id / access ref, use it; else locate via My activities / Find opportunities by title match (reuse a fuzzy title match like Delta's responses_match). Log proactis.activity_match / proactis.already_interested / proactis.express_interest_needed.
- The express-interest + immediate-vs-email branching as specified above, with the pause/resume for the email case and no-pause for already-interested.
- download_documents: read BOTH document sections, download each (direct-link primary, click fallback), return a DownloadResult with fully-populated files (readable path, filename, mime/type, size, source_url=the doc href or activity URL) — so the shared persistence + content extraction record them.
- Session release on terminal state (mirror Delta).
- Robust waits (poll rendered DOM for the documentation table / activity content), distinguish "no documents yet (email pending)" from "not rendered". Clear logs throughout (proactis.* events).

================================================================
BRIDGE (only if needed)
================================================================
If a direct-URL in-session download helper is required (Part DOWNLOAD STRATEGY step 1), add ONE generic token-protected endpoint to browser-bridge/bridge/server.py (e.g. /session/{slug}/download-url {url}) that fetches the URL in the authenticated context and captures it as a download, returning {path, suggested_filename}. Keep it generic (not Proactis-hardcoded). Mirror on BridgeClient. If clicking the link already triggers expect_download cleanly, skip this and reuse the click path.

================================================================
TESTS (mocked bridge, no network, no real Proactis)
================================================================
- is_authenticated: logged-in marker present → True; login page → False.
- locate_tender: activity found by id and by title match; not found → clear state.
- already-interested → no pause, goes to fetch.
- express-interest IMMEDIATE → proceeds to documents.
- express-interest EMAIL-CONFIRMATION → pauses with the awaiting-buyer-release state and clear detail; resume later with activity present → fetches. (Test both the pause and the resume.)
- download_documents: mocked activity DOM with BOTH a documentation table (N rows) and a terms & conditions list (M rows) → all N+M files captured, named from titles, extension from filename/Type, deduped, capped; assert BOTH sections are read.
- direct-link path AND click fallback both covered.
- files flow through the SHARED _persist_documents + ensure_content_extracted (assert rows in tender_document_files + tender_document_content, same as Delta) — reuse, not a parallel path.
- partial (some downloads fail) → partial status + per-doc failure logged; all-fail with rows seen → clear error, not silent nothing_available.
- Keep ALL existing tests green (Delta, CF, brief engine, content store).

================================================================
VERIFICATION
================================================================
- pytest green; ruff clean (backend + bridge); bridge imports clean; dashboard tsc + build clean (no dashboard changes expected — Proactis docs surface through the same tender pages).
- No real Proactis login in the run. Manual re-test note on the test tender (Knowsley "Dynamic Purchasing System for Landscape Contractors", rfx already interested, 3 documentation files + 1 terms file): log in at the bridge window → adapter locates the activity → fetches all 4 documents from both sections → tender_document_files + tender_document_content rows created → dashboard shows them → brief can be generated. Also note: a tender requiring email confirmation should PAUSE cleanly with the awaiting-buyer-release message, not fail.

================================================================
SHIP
================================================================
Commits:
- feat(adapter): Proactis/ProContract adapter — login, express-interest (immediate + email-confirmation pause/resume + already-interested no-pause), document fetch from both sections
- feat(bridge): in-session direct-URL download helper (only if needed)
- feat(registry): register proactis platform → adapter
- test: Proactis auth/locate/interest-branches/both-document-sections/persistence-reuse/partial

PR title: "feat: Phase 4 Chunk 6 — Proactis / ProContract portal adapter"
Description: explain it follows the proven Delta blueprint reusing the bridge + shared persistence + content store; the confirmed Proactis flow (no 2FA; express interest; immediate-vs-email-confirmation handled with a clean pause/resume; documents are plain links in two sections — Activity documentation + Terms & conditions); the download fallback strategy; the manual test on the Knowsley DPS tender. Sentinel: "Phase 4 Chunk 6 — Proactis adapter. Ready for review."

RULES
- Single PR against main, built from CURRENT main. Reuse the bridge, shared _persist_documents, and ensure_content_extracted — do NOT fork infrastructure or write a Proactis-only persistence/content path. Never submit credentials. Keep the human-judgment pause for express-interest (intent to bid). Only procontract.due-north.com. Chunk-3 caps + sha256 dedup. Mocked tests only. If blocked, "BLOCKED:" at top.

Begin.
