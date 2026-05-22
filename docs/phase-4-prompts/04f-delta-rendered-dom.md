Task: fix the Delta adapter to read documents from the RENDERED DOM, not the static page HTML. Delta's tables are JavaScript-rendered web components (bip-table), so the download links don't exist in the raw HTML the bridge currently returns. Confirmed by live testing + DOM inspection. Autonomous run.

This is prompt 4f — the final Delta fix after live testing reached Stage One but found zero documents. Chunks 1-4 + 4b-4e are merged on main. Everything works EXCEPT document enumeration: the adapter logs in, matches the Responses table, detects already-registered, navigates to Stage One, releases the session cleanly — but finds 0 documents and returns nothing_available.

Required reading first:
- tender_agent/services/portals/adapters/delta_esourcing.py (esp. _parse_document_rows, _parse_responses_rows, download_documents, and how it calls bridge.page_html / page-html)
- tender_agent/services/bridge_client.py (the page_html / page_text methods)
- browser-bridge/bridge/server.py (the /page-html and /page-text endpoints — how they read the page)

================================================================
ROOT CAUSE (confirmed by DOM inspection of the live site)
================================================================

Delta's UI is built with BiP Solutions web components: the tables render as custom elements like <bip-table>, <bip-table-search>, <bip-input> with s:id attributes, populated CLIENT-SIDE by JavaScript after page load.

The adapter currently fetches the page via the bridge's page-html endpoint and regex-parses it for download links (downloadDocument.html?respId=...&supplierListId=...&docId=...) and for Responses-table opportunity links (suppRespStatus.html?id=...&listId=...).

THE PROBLEM: page-html returns the INITIAL server HTML, which contains only the empty <bip-table> shells — the rows (and therefore the download links and opportunity links) are injected later by JavaScript and are NOT in that initial HTML. So:
- On Stage One: the documents table has 22 rows in the RENDERED DOM, but the raw page-html has zero downloadDocument links → _parse_document_rows finds nothing → nothing_available.
- (The Responses-table match currently WORKS only because that page may render fast enough or the link happens to be present — but it's the same fragility and should be made robust too.)

PROOF from live inspection:
- Viewing raw page source and searching "downloadDocument" → 0 matches.
- Hovering a row's "Download File" button → the browser status bar shows the real link:
  downloadDocument.html?respId=1038167190&supplierListId=1036629453&docId=1036682119
- So the links DO exist in the rendered DOM, just not the static HTML. The IDs are fully deterministic: respId + supplierListId are already known (from the Responses match / Stage One URL); only docId varies per row.

================================================================
THE FIX
================================================================

PART A — bridge: return RENDERED DOM, with a wait
The bridge must be able to return the rendered DOM after the dynamic table has populated, not the initial HTML.

1. Add a bridge endpoint (or extend the existing page-html) to return rendered HTML:
   POST /session/{slug}/rendered-html  { wait_for_selector?: string, wait_for_text?: string, timeout_ms?: int }
   Behaviour: using the Playwright page, optionally wait for a selector OR for page content to contain a given text (e.g. "downloadDocument" or a row indicator), with a sensible timeout (e.g. 15s), then return document.documentElement.outerHTML (the RENDERED DOM, i.e. page.content() after the wait, or evaluate("document.documentElement.outerHTML")).
   - If the wait condition times out, still return whatever is rendered (don't hard-fail) plus a flag wait_satisfied:false so the adapter can decide.
   - Token-protected like all bridge endpoints.
2. Mirror on BridgeClient: async def rendered_html(slug, *, wait_for_selector=None, wait_for_text=None, timeout_ms=15000) -> str (or a small result with the html + wait_satisfied).
   Keep the existing page_html/page_text methods as-is (other callers may use them).

PART B — adapter: use rendered DOM for both Delta tables
In delta_esourcing.py, replace the raw page-html reads with rendered-DOM reads for the two dynamic tables:

3. Stage One documents: before enumerating, call bridge.rendered_html with a wait for the documents to appear. Wait condition: page content contains "downloadDocument" (the most reliable signal the rows have rendered with their links), OR a selector for a populated row. Use a fallback: if "downloadDocument" doesn't appear within timeout, also try waiting for the file-type text (e.g. "application/vnd") or the "items per page" control to confirm the table rendered, then re-read.
   Then run the existing _parse_document_rows / download_link_regex over the RENDERED html. The download_link_regex (downloadDocument\.html\?respId=(\d+)&supplierListId=(\d+)&docId=(\d+)) is CONFIRMED correct against the live link — it just needs the rendered HTML to match against.

4. Responses table (already-registered match): same treatment — read rendered_html (wait for content containing "suppRespStatus.html" or the "Opportunity" header row to be populated) before _parse_responses_rows. This makes the already-registered match robust rather than relying on fast render.

5. Keep _maximise_page_size: still set the page-size dropdown to max FIRST (so all 22 rows render on one page), THEN read rendered_html with the wait. Order matters: maximise → wait for render → read. If maximise fails, still proceed (default page size) and read what's there; if fewer than total rows are found, log it (delta.documents_partial) but download what was found rather than returning nothing.

6. Robustness: if after the wait the rendered HTML STILL yields zero document rows, return nothing_available WITH a clear detail ("Stage One documents table did not render any downloadable rows — Delta may have changed its page structure") rather than a silent empty result. And log the count of rows the parser saw vs. the "N items" count Delta displays (parse the "22 items" text) so any future mismatch is visible.

PART C — deterministic URL construction (belt-and-braces)
Since respId and supplierListId are already known before download (from the Stage One ids), and the only per-row variable is docId:
7. Extract docId per row from the rendered DOM. The docId may appear in: the download link href (downloadDocument...&docId=NNN), or a row data attribute / onclick. Prefer the href. Build each download URL from DELTA_URLS["document_download"] % (resp_id, list_id, doc_id) — do NOT depend on the href being a complete absolute URL; reconstruct it from the known ids + docId. This makes downloads work even if Delta renders relative or JS-built hrefs, as long as the docId is discoverable.
   If a docId cannot be found for a row but the row clearly is a document (has a title + file-type + size), log delta.docid_missing with the row title so we can see which row and why.

================================================================
TESTS (mocked bridge, no network, no real Delta)
================================================================
- bridge rendered_html: returns rendered HTML; respects wait_for_text (mock a page that "renders" the links only after the wait); times out gracefully with wait_satisfied:false.
- download_documents: given rendered HTML containing 22 downloadDocument links → extracts all 22 docIds, builds 22 correct download URLs from known resp/list ids, downloads via mocked bridge, caps + sha256 dedup applied.
- download_documents: rendered HTML with the empty bip-table shell (no links) even after wait → returns nothing_available with the clear detail (not a silent empty).
- responses match: rendered HTML used; match still works; assert it reads rendered_html not raw page_html.
- partial render (e.g. 10 of 22 rows) → downloads the 10, logs delta.documents_partial, does not return nothing_available.
Keep all existing tests green (existing 48 targeted + full suite).

================================================================
VERIFICATION
================================================================
- pytest green; ruff clean; bridge imports clean; dashboard tsc + build clean (no TS changes expected).
- No real Delta login (human-only 2FA). Real end-to-end re-test is the user's manual step on tender 3169 (286EVX23TV, already registered, OPEN until 12 June) — expected: Response Manager → Responses row → Stage One → wait for bip-table render → enumerate 22 docs → download → release session → files land in ~/.tender-agent/documents.

================================================================
SHIP
================================================================
Commits:
- feat(bridge): rendered-html endpoint with wait-for-selector/text (read DOM after JS render)
- fix(adapter): read rendered DOM for Delta's bip-table tables (documents + responses); deterministic download-URL build from known ids + docId
- fix(adapter): clear nothing_available detail + row-count vs items-count logging; partial-render handling
- test: rendered-html wait + document enumeration from rendered DOM + partial/empty cases

PR title: "fix: Phase 4 Chunk 4f — Delta documents via rendered DOM (bip-table JS render)"
Description: explain the root cause (bip-table client-side rendering; download links absent from static HTML, present in rendered DOM — proven by live DOM inspection showing the downloadDocument link on hover), the fix (rendered-html with wait + deterministic URL build), and the manual re-test on 3169. Sentinel: "Phase 4 Chunk 4f — Delta rendered-DOM document enumeration. Ready for review."

RULES
- Single PR against main. Never submit credentials/2FA. Keep the needs_user_confirmation pause and the already-registered/session-release behaviour from 4e unchanged. Only delta-esourcing.com. Chunk-3 caps + dedup. Mocked tests only. If blocked, "BLOCKED:" at top.

Begin.
