Task: correct the Delta eSourcing adapter for the already-registered tender path, and make the bridge release its Delta session when done (Delta allows only ONE session per account). Confirmed by live testing + screenshots. Autonomous run.

This is prompt 4e — a correction after the first live end-to-end Delta test. Chunks 1-4 + 4b + 4c + 4d are merged on main. The full pipeline works: backend → bridge → real Delta → adapter selected → Register-Interest pause → user confirm. It then failed with "could not determine Delta Stage One response/list ids", and we discovered Delta enforces single-session login. Both are now understood and this prompt fixes them.

Required reading first:
- tender_agent/services/portals/adapters/delta_esourcing.py (the adapter)
- tender_agent/services/portal_orchestrator.py (the fetch flow + confirm path)
- tender_agent/services/bridge_client.py (bridge methods)
- browser-bridge/bridge/server.py (bridge endpoints, esp. session close)
- docs/phase-4-prompts/04c-delta-document-flow.md and 04b (the established Delta flow + constants)

================================================================
WHAT LIVE TESTING REVEALED (two issues, both confirmed)
================================================================

ISSUE 1 — already-registered tenders: the adapter can't reach Stage One.
The current adapter assumes a first-time flow: notice page → click Register Interest → Delta redirects to Stage One (suppRespStatus.html?id=...&listId=...) → capture the two ids. But when the org has ALREADY registered interest (common — and exactly the case we hit), Delta does NOT redirect to Stage One from the notice page. Instead the notice page shows "your organisation has already responded to this questionnaire" and clicking Register Interest does not re-open Stage One. So the adapter never gets the id/listId → "could not determine Delta Stage One response/list ids".

THE REAL PATH for an already-registered tender (confirmed by screenshots):
- Go to the Response Manager: https://www.delta-esourcing.com/delta/suppliers/select/addToList.html
- It shows a "Responses" table listing the supplier's registered opportunities (columns: Opportunity, Opportunity Type, Submitted, Submitted Date, Status, Closing Date, Owner).
- Each opportunity NAME in that table is a link, and the link's href ALREADY CONTAINS BOTH IDS:
    https://www.delta-esourcing.com/delta/suppliers/select/suppRespStatus.html?id={RESP_ID}&listId={LIST_ID}
  e.g. for our test tender: suppRespStatus.html?id=1038167190&listId=1036629453
- So the adapter can read id and listId DIRECTLY from the Responses-table link — no title fuzzy-matching, no redirect needed.

ISSUE 2 — Delta allows only ONE session per account (concurrent login blocked).
Confirmed live: "Concurrent Logins Are Not Enabled — The login details provided are currently in use." While the bridge holds a Delta session, the USER is locked out of Delta (and vice versa). The bridge currently does not release its Delta session after a fetch, so the user stays locked out and must use Delta's "End Session" email to recover. This must be fixed: the bridge releases the Delta session when the fetch completes (or fails), so the single session is freed for the user.

================================================================
NEW UNIFIED LOCATE LOGIC (always check Responses table first)
================================================================

Rewrite the Delta locate/navigate flow to ALWAYS check the Responses table first. This handles both already-registered and not-yet-registered cases with one consistent path and avoids re-registering something already registered (which is what tangled the session).

In delta_esourcing.py, the flow becomes:

1. extract_access_code(tender) — unchanged (the three URL patterns).
2. After authentication (human login via bridge — unchanged), navigate to the Response Manager:
   DELTA_URLS["response_manager"] = "https://www.delta-esourcing.com/delta/suppliers/select/addToList.html"
3. Read the Responses table. For each row, read the opportunity link href and parse the Stage One ids with:
   STAGE_ONE_LINK_REGEX = r"suppRespStatus\.html\?id=(\d+)&listId=(\d+)"
4. Determine whether THIS tender is already in the table:
   - Match the row to the tender. Primary match: if the access code maps to a known id we can correlate — but since the table link gives id/listId directly and the table is typically short (the supplier's own registered opportunities), match by the opportunity TITLE (normalised compare against the tender title) AND/OR by the closing date. If exactly one row, and it plausibly matches, use it. If multiple rows, pick the best title match. Log what matched.
   - If a matching row is found → ALREADY REGISTERED: capture respId/listId from that row's link. Skip Register Interest entirely. Go straight to step 6.
5. If NO matching row is found → NOT YET REGISTERED:
   - This is the interpretive gate. The orchestrator must already have the needs_user_confirmation pause BEFORE this (keep it). After the user confirms, navigate to the notice page (respondToList.html?accessCode={CODE}) and click REGISTER INTEREST. Delta then adds it to the Responses table and/or redirects to Stage One. Re-read the Responses table (or the redirect URL) to capture respId/listId.
   - If after registering we still can't get the ids, fail with a clear detail: "Registered interest but could not locate Stage One page — Delta may require a moment; retry."
6. With respId + listId known, navigate to Stage One:
   DELTA_URLS["stage_one"] = "https://www.delta-esourcing.com/delta/suppliers/select/suppRespStatus.html?id=%s&listId=%s"
7. download_documents (unchanged from 4c): on Stage One, maximise the page-size dropdown, enumerate the documents table, extract each docId via the download_link_regex, and GET each via document_download URL through the authenticated bridge session. Apply caps + sha256 dedup + sanitised filenames.

IMPORTANT re the needs_user_confirmation pause: it represents intent to bid (Register Interest). For an ALREADY-registered tender, the user has ALREADY expressed that intent previously — so re-confirming isn't strictly necessary. BUT keep it simple and safe: still pause for confirmation before the FIRST fetch of a tender (the user is choosing to pull documents / pursue), and on confirm:
   - if already registered → go straight to Stage One (no Register Interest click)
   - if not registered → click Register Interest, then Stage One
This keeps "machines act, humans interpret" intact while not re-registering.

================================================================
SESSION RELEASE (bridge)
================================================================

8. Bridge: ensure there is a clean way to END the Delta session, and the orchestrator calls it when a Delta fetch finishes (success OR failure).
   - browser-bridge: the persistent context for platform_slug='delta_esourcing' — add/confirm a close/end-session path. Two layers:
     (a) A bridge endpoint to close the session's pages and, importantly, LOG OUT of Delta if possible (navigate to Delta's logout, if there's a logout URL/link) so Delta frees the single session — not just close the browser context locally (closing locally may leave Delta's server-side session active until timeout).
     (b) If a clean Delta logout URL isn't reliably known, at minimum close the context AND document that the user may need Delta's "End Session" email if they hit concurrent-login. (Best-effort logout first; the README note as fallback.)
   - Add DELTA_URLS["logout"] if a logout endpoint is known (investigate: Delta supplier logout is typically a "Logout"/"Sign out" link in the logged-in menu, or a /delta/logout.html style URL). If discoverable from the logged-in page's menu, use it. If not certain, click the logout control by its menu label.
   - The orchestrator: after a Delta fetch task reaches a terminal state (complete/failed/error), call the bridge to end the Delta session. Log "delta.session_released".
   - This directly resolves the concurrent-login lockout we hit.

9. Also: when a fetch STARTS, if the bridge already holds a Delta session that's stale/locked, the adapter should handle the "Concurrent Logins Are Not Enabled" page gracefully — detect that page text ("Concurrent Logins Are Not Enabled" / "currently in use") and fail with a clear, actionable detail ("Delta session conflict — another Delta login is active. End your other Delta session, then retry."), rather than hanging or a vague error.

================================================================
CONSTANTS (add/confirm)
================================================================
DELTA_URLS:
  response_manager = "https://www.delta-esourcing.com/delta/suppliers/select/addToList.html"   # CONFIRMED — Responses table lives here
  stage_one        = "https://www.delta-esourcing.com/delta/suppliers/select/suppRespStatus.html?id=%s&listId=%s"  # CONFIRMED
  respond_landing  = "https://www.delta-esourcing.com/delta/respondToList.html?accessCode=%s"   # CONFIRMED (notice page)
  document_download= "https://www.delta-esourcing.com/delta/suppliers/response/overview/documents/downloadDocument.html?respId=%s&supplierListId=%s&docId=%s"  # CONFIRMED
  logout           = (investigate from the logged-in menu; set if confidently known, else use the menu "Logout" control)
DELTA_SELECTORS:
  responses_table              = the "Responses" table on response_manager
  responses_opportunity_link   = the opportunity-name link in each row (href matches STAGE_ONE_LINK_REGEX)
  STAGE_ONE_LINK_REGEX         = r"suppRespStatus\.html\?id=(\d+)&listId=(\d+)"   # CONFIRMED — ids are in the link
  concurrent_login_text        = "Concurrent Logins Are Not Enabled"             # CONFIRMED — detect + clear error
  register_interest_button_text= "REGISTER INTEREST"                              # CONFIRMED (kept)
  open_marker_text             = "currently OPEN"                                 # CONFIRMED (kept)
  download_link_regex          = r"downloadDocument\.html\?respId=(\d+)&supplierListId=(\d+)&docId=(\d+)"  # CONFIRMED (kept)

================================================================
TESTS (mocked bridge, no network, no real Delta)
================================================================
- locate: Responses table contains a row whose link matches the tender → already-registered path; respId/listId parsed from the link; Register Interest NOT clicked; proceeds to Stage One.
- locate: Responses table empty / no match → not-registered path; after (mocked) confirm + Register Interest, ids captured; proceeds.
- concurrent-login page detected → task fails with the clear actionable message.
- session release: after a terminal state, the orchestrator calls the bridge end-session; assert it's called on both success and failure.
- download: unchanged 4c behaviour still passes (enumerate docs, direct GET, caps, dedup).
Keep all existing tests green.

================================================================
SHIP
================================================================
Commits:
- fix(adapter): already-registered path — read Stage One ids from Responses-table link; check Responses first
- fix(adapter): detect Delta concurrent-login page; clear actionable error
- fix(bridge+orchestrator): release/log out Delta session on terminal state (single-session constraint)
- test: already-registered + concurrent-login + session-release

PR title: "fix: Phase 4 Chunk 4e — Delta already-registered path + session release"
Description: explain both issues from live testing (Stage One ids via Responses table for already-registered tenders; Delta single-session lockout) and the fixes. Note: re-test on tender 3169 (already registered, code 286EVX23TV, OPEN until 12 June) — should now go Response Manager → Responses row → Stage One → download 22 docs, then release the session. Sentinel: "Phase 4 Chunk 4e — Delta already-registered + session release. Ready for review."

RULES
- Single PR against main. Never submit credentials/2FA. Keep the needs_user_confirmation pause before first fetch. Never auto-register a tender that's already registered. Only delta-esourcing.com. Chunk-3 caps + dedup. Mocked tests only. If blocked, "BLOCKED:" at top.

Begin.
