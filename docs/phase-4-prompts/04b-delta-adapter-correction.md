Task: correct the Delta eSourcing adapter (built in PR #37) to match Delta's REAL behaviour, confirmed by manual reconnaissance. Also fix the Docker image so scripts/ ships inside the container. Autonomous run.

This is prompt 4b in the Phase 4 sequence — a correction to chunk 4 (#37, merged). Chunks 1-4 are on main.

Required reading first:
- docs/phase-4-prompts/04-browser-bridge-and-delta.md (the original, now partly WRONG — see corrections below)
- tender_agent/services/portals/adapters/delta_esourcing.py (the adapter to fix)
- tender_agent/services/portal_orchestrator.py, services/bridge_client.py, the browser-bridge/ component

================================================================
WHAT WE LEARNED FROM REAL DELTA (corrections to the original spec)
================================================================

The original prompt 4 assumed the adapter would SEARCH Delta for a tender. THAT IS WRONG. Delta has no supplier-side tender search. Corrections, all confirmed against the live site with a real account:

1. NO SEARCH. The access code comes from the tender notice we ALREADY ingest from FTS/CF. Every Delta tender notice contains the code. The adapter must extract the code from the tender's own data, never search Delta.

2. THREE URL PATTERNS appear in tender notices, all ending in the access code (except the legacy numeric one):
   - https://www.delta-esourcing.com/respond/{CODE}                         e.g. /respond/286EVX23TV
   - https://www.delta-esourcing.com/tenders/{description}/{CODE}           e.g. .../W5C25992M5
   - https://www.delta-esourcing.com/delta/respondToList.html?noticeId={NUMERIC}   (legacy, numeric id)
   The {CODE} is an alphanumeric token like 286EVX23TV, W5C25992M5, AB3SUXP78A. Extract it with a regex that pulls the trailing path segment from /respond/ or /tenders/.../ URLs, and the noticeId query param from the legacy form.

3. THE ACCESS-CODE-SUBMIT FLOW (the real entry path):
   - Logged-in suppliers land on the Response Manager at:
     https://www.delta-esourcing.com/delta/suppliers/select/addToList.html
   - There is an "Access Code" text input and a "Submit" button.
   - Enter the code, click Submit.
   - If the tender is open: it's added to the supplier's "Responses" table on the same page, and documents become reachable.
   - If closed: the page shows an error banner with the literal text "This opportunity is not currently open." The adapter MUST detect this exact string and return DownloadResult(status='error', detail='Tender is closed on Delta ("not currently open").'). This is a common, expected state — handle it gracefully, not as a crash.

4. LOGIN IS BY-HAND, WITH MICROSOFT AUTHENTICATOR (app-based 2FA). The machine NEVER submits credentials or 2FA codes. The visible-browser human-login flow from chunk 4 is correct and unchanged: orchestrator opens the session, sets status waiting_for_login, the human logs in (password + Microsoft Authenticator on their phone) in the visible window, the adapter resumes once logged in. Because of the authenticator, SESSION REUSE is critical — see item 8.

5. REAL CONSTANTS to set in DELTA_URLS / DELTA_SELECTORS (replacing the chunk-4 guesses):
   DELTA_URLS:
     - login: the Delta supplier login URL (the adapter navigates here for the human to log in). Confirmed base: https://www.delta-esourcing.com/  — supplier login is reachable from the homepage "Login" / via the response link redirect. Use https://www.delta-esourcing.com/delta/suppliers/select/addToList.html as the post-login landing; if not authenticated Delta redirects to its login page automatically.
     - response_manager: https://www.delta-esourcing.com/delta/suppliers/select/addToList.html
   DELTA_SELECTORS:
     - access_code_input: the "Access Code" text field on the Response Manager page  [CONFIRM SELECTOR — see morning dry-run placeholder #1]
     - submit_button: the "Submit" button next to it  [CONFIRM SELECTOR — placeholder #2]
     - not_open_error_text: "This opportunity is not currently open."  (literal match — CONFIRMED)
     - logged_in_marker: presence of the supplier name / "Supplier Administrator" text + the left menu (Profile Manager, Response Manager, Select Accredit, Resources, Settings) indicates an authenticated session. Use a stable element from that menu.
     - responses_table: the "Responses" table that lists added opportunities  [CONFIRM SELECTOR — placeholder #3]
     - documents_area / document_links: WHERE THE DOCUMENTS LIVE on an opened tender — [TO BE FILLED FROM MORNING DRY-RUN — placeholder #3, the one screen not yet seen]
     - express_interest_button: the control that signals intent to bid — exact label TBD from dry-run. May be "Express Interest" or "Register Interest" or similar.  [TO BE FILLED FROM MORNING DRY-RUN]

6. THE EXPRESS-INTEREST PAUSE (strict principle) is unchanged from chunk 4: the orchestrator must NOT auto-click Express Interest. If documents are gated behind it, set the task to needs_user_confirmation and wait for the user's explicit Confirm. Keep this exactly as built.

================================================================
PART A — FIX THE DOCKER IMAGE (scripts/ not shipped)
================================================================

7. The Dockerfile copies src/ but not scripts/, so backfill_portal_discovery.py (and future scripts) aren't in the container — we had to docker cp it manually. Fix the Dockerfile to COPY the scripts/ directory into /app/scripts/ in the image. Verify `docker compose exec app ls /app/scripts/` lists the backfill script after rebuild.

================================================================
PART B — CORRECT THE ADAPTER
================================================================

8. Rewrite delta_esourcing.py per the real flow above:
   - matches_url: delegates to platform domain_patterns (already correct).
   - extract_access_code(tender) -> str | None: parse the tender's source_url AND description for the three URL patterns; return the code (or noticeId). Add unit tests with the REAL examples: 286EVX23TV, W5C25992M5, AB3SUXP78A, and a legacy respondToList.html?noticeId=1032668140.
   - login_url / is_authenticated: use the real logged_in_marker (left-menu element). Navigate to response_manager; if redirected to login or marker absent → not authenticated.
   - locate_tender: do NOT search. Navigate to response_manager, fill access_code_input with the extracted code, click submit_button. Then: if not_open_error_text present → return LocateResult(status='not_found', detail='closed on Delta'). Else confirm the tender appears in responses_table → status='found'.
   - download_documents: from the opened tender, enumerate document links in documents_area, download each via the authenticated bridge session (bridge.click-download or bridge.download), apply chunk-3 caps + sha256 dedup. [Document-area selectors come from the morning dry-run.]
   - register_interest: performs the Express-Interest click WHEN CALLED — but only ever called after the needs_user_confirmation pause resolves.

9. SESSION REUSE: ensure the bridge's persistent context for platform_slug='delta_esourcing' is reused across fetches so the human authenticates (password + Microsoft Authenticator) as rarely as possible. Add a note/log when a fresh login is required vs. session reused. During verification, document how the adapter behaves when the session is already live (should skip waiting_for_login entirely).

================================================================
PART C — TESTS & VERIFICATION
================================================================

10. Unit tests (mocked bridge, no real Delta):
    - extract_access_code against all real URL patterns above.
    - locate_tender: mocked bridge returns the "not currently open" banner → status not_found, graceful.
    - locate_tender: mocked bridge shows responses_table with the tender → found.
    - download_documents: mocked documents area → files downloaded, caps + dedup applied.
    - is_authenticated true/false from mocked marker presence.
    - the Express-Interest gate triggers needs_user_confirmation (unchanged behaviour, re-assert).

11. Do NOT attempt real Delta login in the autonomous run (no account, and 2FA is human-only). All Delta tests mocked. The real end-to-end test is done by the user manually after merge.

12. Quality gates: full pytest green; bridge tests green; ruff + tsc + npm build clean. Confirm `docker compose exec app ls /app/scripts/` shows the script post-rebuild.

================================================================
PART D — SHIP
================================================================

13. Commits:
    - fix(docker): ship scripts/ inside the app image
    - fix(adapter): Delta real flow — access-code extraction + Response Manager submit + closed-tender handling
    - fix(adapter): real DELTA_URLS/DELTA_SELECTORS constants from live reconnaissance
    - test: Delta access-code extraction + closed-tender + download (mocked)

14. PR title: "fix: Phase 4 Chunk 4b — Delta adapter corrected to real Response-Manager flow"
    Description must include:
    - The "what we learned / what changed from chunk 4" summary (no search; access-code-submit; closed-tender state; 2FA human login).
    - Which selectors are CONFIRMED vs still best-effort pending the user's manual validation.
    - The two live test tenders for the user to validate against: 3169 (code 286EVX23TV) and 3205 (code W5C25992M5).
    - Sentinel: "Phase 4 Chunk 4b — Delta adapter corrected. Ready for review."

================================================================
RULES
================================================================
- Single PR. Never submit credentials or 2FA. Never auto-click Express Interest. Only navigate within delta-esourcing.com domains. Chunk-3 file caps + dedup apply. Mocked tests only. If blocked, "BLOCKED:" at top of PR.

Begin.
