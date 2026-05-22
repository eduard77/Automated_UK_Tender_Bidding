Task: correct two specific mechanisms in the Delta eSourcing adapter to match the REAL site behaviour, confirmed by manual reconnaissance with screenshots. Small, surgical change. Autonomous run.

This is prompt 4c — a final correction to the Delta adapter. PR #39 (chunk 4b) is merged on main. The adapter at tender_agent/services/portals/adapters/delta_esourcing.py currently works ALMOST right, but two mechanisms were built on assumptions that the live site disproved.

Required reading first:
- tender_agent/services/portals/adapters/delta_esourcing.py (the adapter to correct)
- tender_agent/services/portal_orchestrator.py (the login/needs_user_confirmation flow)
- tender_agent/services/bridge_client.py (the bridge methods available)

================================================================
THE TWO THINGS TO FIX (confirmed against live Delta with a real account + screenshots)
================================================================

WRONG ASSUMPTION 1 — "fill an access-code box and click Submit"
The current adapter navigates to the Response Manager, fills an "Access Code" text input, and clicks Submit.
REALITY: the access code rides in the URL. The notice link /respond/{CODE} redirects to:
    https://www.delta-esourcing.com/delta/respondToList.html?accessCode={CODE}
This loads the tender's NOTICE page directly — no form to fill. There is no access-code box step in the real flow. The adapter should navigate straight to respondToList.html?accessCode={CODE} and it lands on the notice page.

WRONG ASSUMPTION 2 — "download documents by clicking three-dot menus"
The current adapter clicks a per-row ⋮ menu then a download item.
REALITY: each document is a direct GET link within the authenticated session. The ⋮ "Download File" action points to:
    https://www.delta-esourcing.com/delta/suppliers/response/overview/documents/downloadDocument.html?respId={RESP_ID}&supplierListId={LIST_ID}&docId={DOC_ID}
The adapter should enumerate these download links from the documents table and GET each directly via the authenticated bridge session — NOT click menus.

================================================================
THE REAL, CONFIRMED END-TO-END FLOW (with screenshots evidence)
================================================================

1. NOTICE PAGE (public, pre-login):
   Navigate to https://www.delta-esourcing.com/delta/respondToList.html?accessCode={CODE}
   This shows the tender notice summary with:
   - A "REGISTER INTEREST" button (exact label: "REGISTER INTEREST")
   - An open/closed banner:
       OPEN  → contains the text "currently OPEN"
       CLOSED → contains the text "not currently open"  (already handled — keep it)
   The documents are NOT visible here. They are gated behind REGISTER INTEREST.

2. REGISTER INTEREST = the interpretive gate (strict principle):
   Clicking REGISTER INTEREST (a) requires login if not already authenticated, and (b) signals intent to bid.
   Per "machines act, humans interpret", the orchestrator must PAUSE here with status needs_user_confirmation and a clear message:
     "Delta requires you to Register Interest in this tender before documents are released. This tells the buyer you intend to bid. Confirm to proceed."
   Only on explicit user Confirm (existing POST /tenders/{id}/fetch-documents/{task_id}/confirm endpoint) does the adapter click REGISTER INTEREST.
   (This pause already exists for the Express-Interest concept — ensure it triggers on the REGISTER INTEREST button, exact label "REGISTER INTEREST".)

3. LOGIN (human-only, unchanged):
   Clicking REGISTER INTEREST when not logged in triggers Delta's login (password + Microsoft Authenticator app 2FA). The human does this in the visible bridge window. The machine never submits credentials or 2FA. Session persists and is reused (already implemented — keep the session-reuse logging).

4. POST-REGISTRATION — STAGE ONE OVERVIEW:
   After Register Interest + login, Delta lands on the "Stage One: Overview" page at:
     https://www.delta-esourcing.com/delta/suppliers/select/suppRespStatus.html?id={RESP_ID}&listId={LIST_ID}
   Capture {RESP_ID} and {LIST_ID} from THIS URL (query params id and listId). These two IDs are needed to build document download URLs.
   The page has three stage tabs: "Stage One: Overview" (download buyer docs — OUR TARGET), "Stage Two: Prepare Response" (upload — not ours), "Stage Three: Submit Response" (submit bid — never ours, stays manual).

5. DOCUMENTS TABLE (on Stage One Overview):
   A table with columns: Document Title | Size | File Type | Uploaded | Action.
   Shows an item count ("22 items | Display N items per page") and a page-size dropdown.
   Each row's Action ⋮ menu contains "Download File" whose link is:
     downloadDocument.html?respId={RESP_ID}&supplierListId={LIST_ID}&docId={DOC_ID}
   Note: respId == the id from step 4, supplierListId == the listId from step 4. Only docId varies per row.

   THE ADAPTER MUST:
   a. Set the page-size dropdown to its maximum so all items are on one page (if the max is below the item count, fall back to paging through).
   b. Enumerate all document rows. For each, extract docId (from the row's download link href, matching downloadDocument.html?...&docId=(\d+)). Also capture the document title and file-type for naming.
   c. For each docId, build the download URL using the captured respId/listId + docId, and GET it through the authenticated bridge session (bridge.download — which uses the session cookies). Do NOT click the ⋮ menu.
   d. Apply existing chunk-3 caps (100MB/doc, 50 docs/tender → note this tender has 22, fine), sanitised filenames (use the document title + correct extension from file-type), and sha256 dedup.
   e. Return DownloadResult with all files, status complete (or partial if some failed).

================================================================
CONSTANTS TO SET (replace current best-effort values)
================================================================

DELTA_URLS:
  respond_landing  = "https://www.delta-esourcing.com/delta/respondToList.html?accessCode=%s"   # CONFIRMED
  legacy_respond   = "https://www.delta-esourcing.com/delta/respondToList.html?noticeId=%s"      # CONFIRMED (legacy numeric)
  stage_one        = "https://www.delta-esourcing.com/delta/suppliers/select/suppRespStatus.html?id=%s&listId=%s"  # CONFIRMED (post-registration)
  document_download= "https://www.delta-esourcing.com/delta/suppliers/response/overview/documents/downloadDocument.html?respId=%s&supplierListId=%s&docId=%s"  # CONFIRMED

DELTA_SELECTORS:
  register_interest_button_text = "REGISTER INTEREST"     # CONFIRMED (button label on notice page)
  open_marker_text   = "currently OPEN"                   # CONFIRMED
  closed_marker_text = "not currently open"               # CONFIRMED (keep existing handling)
  logged_in_marker   = a stable element from the left menu present when authenticated (Profile Manager / Response Manager / Select Accredit / Resources / Settings) plus "Supplier Administrator" near the user name top-right. CONFIRMED these exist; pick a stable one.
  documents_table    = the Stage One documents table (columns Document Title/Size/File Type/Uploaded/Action). 
  page_size_select   = the "Display N items per page" dropdown.
  download_link_regex= r"downloadDocument\.html\?respId=(\d+)&supplierListId=(\d+)&docId=(\d+)"   # CONFIRMED — extract the three IDs

Remove any remaining access-code-text-input / submit-button form-fill logic for the notice step — it does not exist in the real flow. (The bridge fill/click primitives stay in the codebase; they're just not used for Delta's notice step. They may be used to set the page-size dropdown.)

================================================================
TESTS
================================================================
Update/extend the Delta adapter tests (all mocked bridge, no network, no real Delta):
- locate_tender: navigating to respond_landing?accessCode={CODE} → notice page; detects REGISTER INTEREST button; detects open vs closed banner. No form-fill asserted anymore.
- the REGISTER INTEREST gate triggers needs_user_confirmation (assert the orchestrator pauses; never auto-clicks).
- after confirm + (mocked) login, capture respId/listId from a mocked Stage One URL.
- download_documents: mocked documents table with several rows (incl. a 22-row case) → extracts all docIds via download_link_regex, builds correct download URLs, GETs each via mocked bridge.download, applies caps + sha256 dedup + sanitised filenames. Assert NO menu-clicking path is used.
- legacy numeric noticeId still navigates directly.
Keep all existing passing tests green. Target: existing 35 + however many new/changed; full suite must pass.

================================================================
VERIFICATION
================================================================
- pytest green (full backend suite via CI; the Delta + bridge tests locally).
- ruff clean on changed files.
- tsc --noEmit + npm build clean (no TS changes expected).
- No real Delta login in the run (human-only 2FA). Real end-to-end validation is the user's manual step after merge, against tender 3169 (code 286EVX23TV) — which is OPEN until 12 June and which the user has already registered interest in, so its 22 documents are reachable for the real test.

================================================================
SHIP
================================================================
Commits:
- fix(adapter): Delta notice via accessCode URL (remove non-existent access-code form step)
- fix(adapter): download documents via direct downloadDocument.html GET (remove menu-click path); capture respId/listId from Stage One
- test: Delta real document flow (URL-based code, direct-link downloads, register-interest gate)

PR title: "fix: Phase 4 Chunk 4c — Delta document flow corrected to real Stage-One download mechanism"
Description: explain the two corrected mechanisms with the confirmed URLs; note the user validates end-to-end against tender 3169 (286EVX23TV) which is OPEN and already has interest registered. Sentinel: "Phase 4 Chunk 4c — Delta document flow corrected. Ready for review."

RULES
- Single PR, against clean main. Never submit credentials/2FA. Never auto-click REGISTER INTEREST (goes through needs_user_confirmation). Only fetch from delta-esourcing.com. Chunk-3 caps + dedup apply. Mocked tests only. If blocked, "BLOCKED:" at top of PR.

Begin.
