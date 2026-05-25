Task: fix the Delta action-menu selectors and the page-size maximise so document downloads actually fire. The click-download mechanism (4i) is correct and on main, but the ⋮ trigger / "Download File" selectors don't match Delta's real DOM, and the page-size isn't maximised (only 10 of 22 rows seen). Confirmed by live DOM inspection. Autonomous run.

This is prompt 4j — a selector fix after live testing. Chunks 1-4 + 4b-4i are merged on main. The full pipeline now works end-to-end: login (session remembered), Responses match, already-registered skip (no pause), Stage One, enumerates rows, and CALLS click_download_in_row per row. The only failure: "Found 10 documents but could not trigger any downloads — Delta action-menu selector may have changed." So: (a) the action-menu selectors are wrong, and (b) it saw 10 not 22 (page-size not maximised).

Required reading first:
- tender_agent/services/portals/adapters/delta_esourcing.py — DELTA_SELECTORS (esp. row_action_menu / row_download_item / documents_table / document_rows), _maximise_page_size, and download_documents (the per-row click loop calling bridge.click_download_in_row)
- tender_agent/services/bridge_client.py — click_download_in_row signature (table_selector, row_index, menu_trigger_selector, download_item_text, ...)
- browser-bridge/bridge/server.py — the click-download-in-row endpoint (how it resolves the row, clicks the menu trigger, clicks the download item by text, captures expect_download)

================================================================
THE REAL DOM (confirmed by live inspection on tender 3169 Stage One)
================================================================
The documents table:
  <table id="document" class="dataTable no-footer" role="grid">
    <thead>...</thead>
    <tbody>
      <tr class="odd" role="row"> <td>Title</td> <td>Size</td> <td>FileType</td> <td>Uploaded</td>
        <td>
          <bip-actions-menu>
            <div s:id="actionsMenu" class="bip-actions-menu">
              <i s:id="actions_i" class="bip-actions-menu-link fa fa-ellipsis-v" tabindex="-1" placement="bottom" ...></i>
            </div>
          </bip-actions-menu>
        </td>
      </tr>
      <tr class="even" role="row">...</tr>  (alternating odd/even)
      ...
    </tbody>
  </table>

KEY FACTS:
- Rows: tr[role="row"] inside table#document tbody (classes alternate "odd"/"even").
- The ⋮ ACTION TRIGGER is an <i> Font Awesome icon: i.bip-actions-menu-link.fa-ellipsis-v  (NOT a <button>; the old selector likely targeted a button/link and missed). It lives in the row's last <td> inside <bip-actions-menu>.
- INTERACTION (confirmed): click the ⋮ icon → a popup appears (rendered in a body-level div.highslide-container) containing a "Download File" control → click that → the file downloads (Playwright download event fires). It is TWO clicks: ⋮ then "Download File".
- The "Download File" control pops up at BODY level (not inside the row), so it must be matched page-wide by visible text "Download File", not within the <tr>.
- PAGE SIZE: the table shows "22 items | Display [10] items per page" — a standard DataTables length control. It currently shows 10, so only 10 rows are in the DOM. Must be set to show ALL 22 (or 100) before enumerating/clicking.

================================================================
THE FIX
================================================================

1. DELTA_SELECTORS — set the confirmed values:
   documents_table   = "table#document"
   document_rows     = "table#document tbody tr[role='row']"
   row_action_menu (the ⋮ trigger within a row) = "i.bip-actions-menu-link.fa-ellipsis-v"
     (relative to the row: within row index i, find this icon in the row's action cell)
   row_download_item = matched by VISIBLE TEXT "Download File" page-wide (it renders in div.highslide-container at body level after the ⋮ click). The bridge should click it by text, e.g. a Playwright text selector / get_by_text("Download File"), NOT a within-row selector.

2. click_download_in_row (bridge + adapter) must implement the TWO-CLICK interaction:
   - Resolve the row_index-th DATA row of table#document tbody (skip thead; rows are tr[role="row"]).
   - Within that row, click the ⋮: i.bip-actions-menu-link.fa-ellipsis-v.
   - Wait briefly for the popup (div.highslide-container with "Download File") to appear.
   - Then capture the download around clicking the "Download File" text control:
       async with page.expect_download(timeout=...) as dl:
           await page.get_by_text("Download File", exact=False).last.click()   # body-level popup
       download = await dl.value
     (Use .last or a scoping that targets the just-opened popup; if multiple match, prefer the visible one.)
   - Save + return {path, suggested_filename} as before. On no download event → clear 502 so the adapter logs per-row failure and continues.
   - If the bridge endpoint currently clicks the menu trigger then looks for download_item_text WITHIN the row, change it to click the body-level "Download File" by text. Keep the endpoint generic (selectors/text passed from the adapter).

3. _maximise_page_size — make it actually work on this DataTables control:
   - The length control is a <select> (DataTables length menu) near "items per page". Find it (e.g. select[name$='_length'] or the select within the dataTables_length container, or a select whose options include 10/25/50/100). Set it to the LARGEST option (e.g. 100) so all 22 rows render on one page.
   - After changing it, WAIT for the table to re-render to the full row count (poll until tbody row count == the "N items" number parsed from the "22 items" text, or stabilises), THEN enumerate + click.
   - If the select can't be found or maxed, fall back to paging, but FIRST priority is: get all rows on one page. Log delta.page_size_maximised (value set) or delta.page_size_max_skipped (reason).
   - Verify the enumerated row count matches the "N items" header; if it sees fewer than N after maximising, log delta.documents_partial with seen vs expected.

4. download_documents: with selectors fixed and page maximised, iterate ALL rows (expect 22 for 3169), two-click each, capture, name from Title, extension from download/File-Type, caps + sha256 dedup. Partial handling and clear-error-on-all-fail unchanged from 4i.

================================================================
TESTS (mocked bridge, no network, no real Delta)
================================================================
- bridge click-download-in-row: mocked page where clicking the row's i.fa-ellipsis-v then the body-level "Download File" text fires a download → returns path; no event → 502; header rows skipped so row_index maps to data rows.
- adapter: with N mocked rows, maximise sets the length select to the max option and waits for N rows; then two-click each row; assert it clicks i.bip-actions-menu-link.fa-ellipsis-v and the "Download File" text control; all N saved/deduped/capped.
- page-size: select found → set to max, row count grows to N; select missing → logged fallback.
- partial / all-fail-error / empty unchanged.
- Keep ALL existing tests green (4e/4f/4h/4i). Update any test asserting the old action-menu selectors to the confirmed ones.

================================================================
VERIFICATION
================================================================
- pytest green; ruff clean (backend + bridge); bridge imports clean; dashboard tsc + build clean.
- No re
