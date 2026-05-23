Task: change the Delta adapter to download documents by CLICKING each row's action menu (like a human), not by scraping download links from the HTML. The links don't exist in the DOM until the user interacts — confirmed by live inspection. Autonomous run.

This is prompt 4g — the final Delta document-download fix. Chunks 1-4 + 4b-4f are merged on main. Live testing now gets ALL the way through correctly: logs in, reuses session, matches the Responses table, detects already-registered (delta.already_registered), skips Register Interest, reaches Stage One, and ENUMERATES ALL 22 DOCUMENT ROWS (delta.documents_none logs items:22 with every title). The ONLY remaining failure: it can't get a docId/download-link per row, because those don't exist in the static/rendered HTML.

Required reading first:
- tender_agent/services/portals/adapters/delta_esourcing.py (download_documents, _parse_document_rows, the docid extraction, _DOWNLOAD_LINK_RE)
- tender_agent/services/bridge_client.py (existing primitives: click, fill, element_exists, select_option, rendered_html, page_text, navigate, AND any download/file-fetch method)
- browser-bridge/bridge/server.py (the click endpoint, the download mechanism, how Playwright downloads are captured/exposed)

================================================================
ROOT CAUSE (confirmed by live DOM inspection)
================================================================
Delta's Stage One documents table is <table id="document"> inside <div id="documentList">, a bip-table component. Crucially:
- The rows render with title/size/file-type/uploaded/action columns (the adapter ALREADY reads all 22 titles correctly).
- But there is NO per-row downloadDocument.html link in the DOM. Delta uses checkboxes + a "documentIds" cookie (the page sets document.cookie="documentIds=...") and JS. The download URL/link only materialises when the user INTERACTS with the row's Action menu.
- Confirmed interaction: click the row's "three dots" (⋮) Action control → a "Download File" control appears → click it → the browser downloads that file.
So the current approach (regex downloadDocument links out of HTML, or build URLs from a per-row docId) fails because neither the link nor the docId is present without interaction. delta.render_signal_absent (signal "downloadDocument") and delta.docid_missing × 22 confirm this.

================================================================
THE FIX — click to download, capture the file (mimic the human)
================================================================
Replace the link-scraping download path with an interaction-driven one. For each of the N document rows on Stage One:
1. Click that row's Action "three dots" (⋮) control.
2. Click the "Download File" control that appears (a menu item / popup; text "Download File").
3. Capture the file the browser downloads (Playwright exposes downloads via the page "download" event / expect_download). Save it, then process with the EXISTING pipeline (sha256 dedup, size cap, sanitised filename — reuse the row's Document Title for the name, and the file-type column for the extension if the download has none).

This needs bridge support for capturing a click-triggered download. Implement in the bridge:

A. BRIDGE — a primitive that clicks an element and returns the resulting download.
   POST /session/{slug}/click-download  { selector, timeout_ms? }  (or extend the existing click)
   Behaviour: use Playwright's download capture, e.g.:
     async with page.expect_download(timeout=...) as dl_info:
         await page.click(selector)
     download = await dl_info.value
     save to the session download dir (the existing TENDER_AGENT_BRIDGE_DOWNLOAD_DIR / per-session dir), return { path, suggested_filename }.
   Token-protected. Return the saved path (a host path the backend's mounted documents dir can read, OR stream/return bytes — match how the existing document storage reads bridge downloads; reuse the same mechanism the adapter already uses to pick up bridge-downloaded files in chunk-4/4c).
   If no download event fires within timeout, return a clear error so the adapter can log delta.download_click_failed for that row and continue with the others.

B. BRIDGE — robust per-row selectors. Because rows have no stable download link, the adapter must target the Action control by row. Options the bridge/adapter can use:
   - Locate rows via the table (table#document tbody tr), and within each row the Action cell's menu trigger (the ⋮). Then the "Download File" item.
   - Provide a way to act on the Nth row (e.g. an index-based locator) so the adapter can iterate rows 0..N-1 deterministically.
   Add a bridge helper if needed: click the ⋮ in row index i, then click the "Download File" item, then capture the download. Keep it generic (selectors passed from the adapter), not Delta-hardcoded in the bridge.

C. ADAPTER — new download flow in download_documents:
   - Navigate to Stage One (already working: stage_one URL with resp_id/list_id).
   - Maximise page size (existing _maximise_page_size) so all rows are present.
   - Read the rendered table to get the ROW COUNT and each row's Title + File Type (already working — keep this; it gives us names/extensions and the expected count, e.g. 22).
   - For each row index: open the ⋮ menu, click "Download File", capture the download via the bridge click-download primitive. Map the captured file to the row's Title for naming.
   - Apply existing caps + sha256 dedup. Build DownloadResult.
   - If some rows succeed and some fail: status partial, missing_count = failures, log delta.download_click_failed per failed row with its title. Only return nothing_available if ZERO downloaded AND zero rows seen (genuine empty); if rows were seen but all clicks failed, return error/partial with a clear detail naming the problem ("found N documents but could not trigger downloads — Delta action-menu selector may have changed").
   - Keep session release on terminal state (4e) unchanged.

SELECTORS (add to DELTA_SELECTORS; confirm against the live structure):
   documents_table = "table#document"            # CONFIRMED id from live DOM (inside div#documentList)
   document_rows   = "table#document tbody tr"
   row_action_menu = the ⋮ trigger within a row's Action cell  (the three-dots control)
   row_download_item = the "Download File" menu item/popup control (text "Download File")
   Keep the old downloadDocument regex/constants only if still used as a fallback; otherwise remove the dead docid/link path so the code reflects reality.

================================================================
TESTS (mocked bridge, no network, no real Delta)
================================================================
- bridge click-download: mocked Playwright download event → returns saved path + suggested_filename; no event within timeout → clear error.
- adapter download_documents: mocked bridge where each row's click-download yields a file → all N files saved, named from row titles, deduped, capped; assert it iterates per row and uses click-download (NOT link scraping).
- partial: some rows' click-download error → status partial, missing_count correct, per-row failure logged, the rest saved.
- zero rows seen → nothing_available; rows seen but all clicks fail → error/partial with the clear detail (NOT silent nothing_available).
- Keep all existing tests green (rendered-html, responses match, already-registered, session release).

================================================================
VERIFICATION
================================================================
- pytest green; ruff clean; bridge imports clean; dashboard tsc + build clean (no TS changes expected).
- No real Delta login in the run (human-only 2FA). Manual re-test by user on tender 3169 (286EVX23TV, already registered, OPEN until 12 June): expect Stage One → click each ⋮ → Download File → 22 files captured → land in ~/.tender-agent/documents → session released.

================================================================
SHIP
================================================================
Commits:
- feat(bridge): click-download primitive (capture Playwright download from a click)
- fix(adapter): Delta downloads via row action-menu clicks (⋮ → Download File), not link scraping; per-row capture, partial handling, clear errors
- test: click-download capture + per-row download flow + partial/empty cases

PR title: "fix: Phase 4 Chunk 4g — Delta documents via action-menu click-download"
Description: explain the root cause (no per-row download link in the DOM; Delta uses checkboxes/documentIds cookie + JS; download only fires on ⋮ → Download File click — confirmed by live inspection and by the adapter already enumerating all 22 titles but finding zero docIds), and the fix (bridge click-download primitive + adapter clicks each row). Manual re-test on 3169. Sentinel: "Phase 4 Chunk 4g — Delta click-download. Ready for review."

RULES
- Single PR against main. Never submit credentials/2FA. Keep needs_user_confirmation pause, already-registered detection, and session release (4e) unchanged. Only delta-esourcing.com. Chunk-3 caps + sha256 dedup. Mocked tests only. If blocked, "BLOCKED:" at top.

Begin.
