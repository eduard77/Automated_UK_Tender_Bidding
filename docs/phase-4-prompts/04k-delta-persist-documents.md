Task: fix the document-persistence step so fetched Delta files get recorded in the database. The files download to disk correctly, but no tender_document_files rows are written — so the dashboard shows "No documents downloaded yet" and the brief engine can't see them. Autonomous run.

This is prompt 4k — a persistence fix after a successful live fetch. Chunks 1-4 + 4b-4j are merged on main. Delta document download now WORKS end-to-end: a live run on tender 3169 logged delta.documents_enumerated rows:22 items:22 then 22× click-download-in-row 200 OK, and all 22 files landed on disk at the bridge download dir / documents storage. BUT: `select count(*) from tender_document_files where tender_id=3169` returns 0. The files are on disk; the DB has no record of them. The disk→DB-record step is missing or not being called.

Required reading first:
- tender_agent/services/portal_orchestrator.py — the post-download persistence path. In chunk 3 the ORCHESTRATOR (not the adapter) took the adapter's DownloadResult.files, computed sha256, deduped on (tender_id, sha256), and inserted tender_document_files rows. Find that code and check whether the Delta/click-download path actually reaches it.
- tender_agent/services/portals/adapters/delta_esourcing.py — download_documents: what it returns now. Confirm it returns a DownloadResult with a populated files list (list of DownloadedFile with path/filename/mime_type/size_bytes/source_url) after the click-download rewrite (4i/4j). The rewrite may have changed the return shape so the orchestrator's persistence no longer picks the files up.
- tender_agent/services/bridge_client.py — click_download_in_row return type (BridgeRowDownload: path, suggested_filename, ...). Confirm how the saved file path crosses from the bridge download dir into something the backend can read (the shared bridge-downloads volume from chunk 4) and into the DownloadedFile records.
- The tender_document_files model + the chunk-3 migration (0007) that created it — exact columns: tender_id, filename, storage_path, source_url, mime_type, size_bytes, sha256, fetched_at, fetched_via_platform_id, created_at, updated_at.
- The CF adapter (contracts_finder_direct.py) + how its DownloadResult flowed into persistence in chunk 3 — the PROVEN-working reference for how files get recorded. Mirror that.

================================================================
ROOT CAUSE (to confirm, then fix)
================================================================
In chunk 3, the flow was: adapter.download_documents → DownloadResult(files=[DownloadedFile...]) → orchestrator computes sha256 per file, dedups on (tender_id, sha256), inserts tender_document_files rows, sets fetched_via_platform_id. The dashboard reads tender_document_files; the brief engine reads documents from the DB.

When 4i/4j rewrote Delta's download_documents to the click-download mechanism, the persistence link broke. Likely one of:
(a) download_documents now saves files via the bridge but returns a DownloadResult whose files list is empty or shaped differently, so the orchestrator's persistence loop iterates nothing.
(b) The click-download path returns bridge-side paths (in the bridge download dir) that the orchestrator's persistence doesn't translate to readable container paths (the shared volume), so it can't sha256/copy them and skips.
(c) The Delta path in the orchestrator returns early (e.g. on the already-registered/no-pause branch) and never reaches the persistence code that the CF path uses.

INVESTIGATE which, then fix so the Delta click-download path persists exactly like CF did. Add a log event at the persistence step (e.g. delta.documents_persisted count=N, or orchestrator.documents_persisted) so it's visible in logs — the absence of any such event in the live run is itself a clue.

================================================================
THE FIX
================================================================
1. Ensure download_documents returns a DownloadResult with a fully-populated files list: each DownloadedFile must carry the readable file path (a path the BACKEND container can open — i.e. under the mounted bridge-downloads volume, not a Windows-only path), filename (from the row Title, sanitised), mime_type (from the download or the File Type column), size_bytes, source_url (best-effort: the Stage One URL or a per-doc identifier; if no per-doc URL exists, use the tender Stage One URL + doc title so the column is non-null per the schema).

2. Ensure the orchestrator's persistence step RUNS for the Delta click-download path and writes tender_document_files rows:
   - For each DownloadedFile: compute sha256, dedup on (tender_id, sha256) (skip if exists), copy/move the file from the bridge-downloads volume into the canonical documents storage (the chunk-3 layout under TENDER_AGENT_DOCUMENTS_DIR, the same place CF stored), insert a tender_document_files row (tender_id, filename, storage_path RELATIVE to the documents dir, source_url, mime_type, size_bytes, sha256, fetched_at=now, fetched_via_platform_id=delta platform id).
   - Apply the chunk-3 caps (100MB/doc, 50 docs/tender) and the same secure_filename sanitisation.
   - This must be the SAME persistence routine CF uses — if CF persistence lives in a shared orchestrator method, route Delta through it; do not write a parallel Delta-only persistence that can drift.
   - Log delta.documents_persisted (or orchestrator.documents_persisted) with the count inserted and the count deduped/skipped.

3. Storage path crossing: the bridge writes downloads to the shared bridge-downloads host folder (mounted into the backend per chunk 4). Confirm the path the adapter puts in DownloadedFile.path is the BACKEND-CONTAINER path under that mount (e.g. /app/data/bridge-downloads/...), and that the orchestrator reads from there. If the click-download return gives only a bridge-relative path or filename, translate it to the container-readable path before persistence. If the shared-volume wiring is missing for this path, fix it so the backend can read the bridge's downloaded files.

4. fetched_via_platform_id: set it to the delta_esourcing platform's id (look it up by slug) so the dashboard can show which platform fetched the docs, matching chunk-3 behaviour.

5. Re-fetch idempotency: a second fetch of the same tender must not duplicate rows (sha256 dedup), exactly as chunk 3 guaranteed.

================================================================
TESTS (mocked bridge, no network, no real Delta)
================================================================
- download_documents returns a DownloadResult with N DownloadedFile records carrying readable paths/filenames/sizes/mime/source_url (mocked click_download_in_row yielding files) — assert the files list is populated and well-formed.
- orchestrator persistence for the Delta path: given a DownloadResult with N files on a readable temp dir, assert N tender_document_files rows are inserted with correct columns (tender_id, sha256, storage_path, fetched_via_platform_id=delta id), files copied into the documents storage; assert it uses the SAME persistence routine as CF.
- dedup: running persistence twice with the same files inserts rows only once.
- caps: a file >100MB rejected; >50 docs capped — consistent with chunk 3.
- already-registered/no-pause branch: assert this branch ALSO reaches persistence (guard against root-cause (c)).
- Keep ALL existing tests green (4e/4f/4h/4i/4j; CF persistence tests).

================================================================
VERIFICATION
================================================================
- pytest green; ruff clean (backend + bridge); bridge imports clean; dashboard tsc + build clean.
- No real Delta login in the run. Manual re-test on tender 3169 (286EVX23TV, already registered): fetch → 22 files downloaded AND `select count(*) from tender_document_files where tender_id=3169` returns 22 (or the deduped count) → dashboard /tenders/3169 shows the documents listed with their real filenames (ITT - London Surrey.docx, Quality questionnaire.docx, Price list 2026.xlsx, etc.) and working download links.

================================================================
SHIP
================================================================
Commits:
- fix(adapter): download_documents returns fully-populated DownloadResult (readable paths, filenames, mime, size, source_url) for the click-download path
- fix(orchestrator): Delta click-download path persists tender_document_files via the shared chunk-3 persistence routine (sha256 dedup, caps, fetched_via_platform_id, container-readable paths); persistence log event
- test: Delta persistence (rows written, dedup, caps, already-registered branch reaches persistence)

PR title: "fix: Phase 4 Chunk 4k — persist fetched Delta documents to the database"
Description: explain the symptom (22 files on disk, 0 rows in tender_document_files, dashboard shows nothing, brief engine blind), the root cause found (which of a/b/c), and the fix (route the Delta click-download path through the same persistence the CF adapter uses). Manual re-test on 3169 expecting 22 rows + dashboard listing. Sentinel: "Phase 4 Chunk 4k — Delta document persistence fixed. Ready for review."

RULES
- Single PR against main, built from CURRENT main (which has 4j). Do NOT change 4h/4i/4j download behaviour — only ensure the downloaded files get RECORDED. Reuse the chunk-3 persistence routine; do not write a divergent Delta-only one. Never submit credentials/2FA. Keep already-registered skip, session release. Only delta-esourcing.com. Chunk-3 caps + sha256 dedup. Mocked tests only. If blocked, "BLOCKED:" at top.

Begin.
