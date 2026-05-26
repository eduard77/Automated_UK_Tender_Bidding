Task: build (1) a document-content store so each fetched document's extracted text lives in the database as reusable data — fetched once, reused forever, never re-downloaded to be used again — and (2) the bid-brief generation engine that reads that stored content, uses Claude (the Anthropic API) to analyse it, and produces a 2-page brief leading with a BID / NO-BID recommendation and key risks. Autonomous run. Land as an OPEN PR (do not merge) — the user is away; review + live-test Friday.

This is prompt 5 in the Phase 4 build sequence. Chunks 1-4 + 4b-4k merged on main (4k = document persistence: tender_document_files rows with filename, storage_key, content_type, size_bytes, sha256, url per tender). Build from current main.

DESIGN INTENT (important): document CONTENT is the durable, reusable asset — not the files on disk. The files currently live on one machine; their content must become database data so it can be reused without re-downloading (and later moved to a cloud DB and used for cross-tender comparison). This prompt makes the extracted content first-class DB data. The actual files staying or being cleaned up is a SEPARATE later decision — do NOT delete files here; just stop treating them as the only home for content.

Required reading first:
- tender_agent/db/models.py (or models.py) — TenderDocumentFile (4k) + Tender models
- tender_agent/services/portal_orchestrator.py — _persist_documents (4k), the fetch flow, storage_key layout under TENDER_AGENT_DOCUMENTS_DIR
- tender_agent/api/tender_fetch.py — the async background-task pattern (fetch_tasks) to mirror for brief generation
- tender-agent-dashboard/components/TenderDetail.tsx — the existing "BRIEF DRAFT / Generate brief" panel + the documents panel; lib/api.ts; the Elevated Genera theme
- latest migration number (0008 on main; 4k may have added 0009 — use the next free number)

================================================================
PART A — DOCUMENT CONTENT STORE (the reuse foundation)
================================================================
1. Migration NNNN: table tender_document_content
   - id (bigserial PK)
   - document_file_id (bigint FK tender_document_files ON DELETE CASCADE, not null)
   - tender_id (bigint FK tenders ON DELETE CASCADE, not null)   // denormalised for easy per-tender + cross-tender queries (comparison later)
   - sha256 (text not null)                                       // the document content hash from the file record
   - extracted_text (text nullable)                               // the document's text content
   - char_count (integer nullable)
   - extraction_status (text not null)                            // ok | empty | unsupported | error
   - extraction_detail (text nullable)                            // reason when not ok
   - doc_type (text nullable)                                     // docx | xlsx | pdf | zip-member | txt | csv
   - extractor_version (text not null)                            // bump when extraction logic changes, to allow re-extraction
   - created_at, updated_at
   - UNIQUE (sha256, extractor_version)   // same content + same extractor = extract ONCE, reuse everywhere (across tenders too)
   - INDEX (tender_id)
   ORM model TenderDocumentContent; pydantic schemas.

2. tender_agent/services/brief/document_extractor.py:
   - Extract text from a stored document file (read via storage_key under TENDER_AGENT_DOCUMENTS_DIR): .docx (python-docx), .xlsx (openpyxl — per-sheet, include sheet names + cell values), .pdf (pdfplumber/pypdf; scanned/no-text → status 'empty' with detail, do not fail), .zip (extract + recurse into members; each member can get its own content row as a zip-member), .txt/.csv (plain). Unknown → 'unsupported'. Never throw on a bad file; return per-file {text, char_count, status, detail, doc_type}.
   - Add python-docx, openpyxl, pdfplumber to pyproject.toml if missing.

3. Content store service (in the extractor module or a small content_store.py):
   - ensure_content_extracted(db, tender) → for each TenderDocumentFile of the tender:
       * If a tender_document_content row already exists for (sha256, current extractor_version) → REUSE it (do NOT re-read the file, do NOT re-download). If it exists for the same sha256 but a DIFFERENT tender (same document seen on another tender), copy/reference the extracted_text into a row for this tender (or link) so content fetched once is reused across tenders — never re-extract identical content.
       * Else extract from the file and insert a tender_document_content row.
   - Returns the set of content rows for the tender. Log brief.content_extracted (extracted=N reused=M) so reuse is visible.
   - This is the mechanism that delivers "even if the ITT was already fetched, use it without downloading again": content keyed by sha256, extracted once, reused.

4. Hook extraction into the fetch flow: after _persist_documents records the files, call ensure_content_extracted so content is captured at fetch time (not only at brief time). Brief generation also calls it (idempotent) as a safety net. Extraction failures for one doc must NOT fail the fetch — record the per-doc status and continue.

================================================================
PART B — LLM ANALYSIS LAYER
================================================================
5. tender_agent/services/brief/llm_client.py — thin async Anthropic Messages API client:
   - API key from env ANTHROPIC_API_KEY; model from env BRIEF_LLM_MODEL, default constant "claude-sonnet-4-6" (single named constant).
   - Missing key → clear error "ANTHROPIC_API_KEY not set — add it to the backend .env to generate briefs". API error/timeout → retry once then fail with a clear message.
   - MUST be injectable/mockable. ALL tests use a fake client returning canned JSON — NO real API calls in CI, zero cost during the build.

6. tender_agent/services/brief/brief_generator.py:
   - Input tender_id → load tender + its tender_document_content rows (the STORED content; do not re-read files unless content is missing).
   - Token budget (env BRIEF_MAX_INPUT_TOKENS, default ~150k): if content exceeds budget, prioritise ITT/instructions + quality-questionnaire + specification docs (filename keyword match), truncate large spreadsheet dumps (structure + sample). Record per-doc included full|truncated|omitted.
   - System prompt: expert UK construction bid manager analysing an ITT pack for an SME contractor; return STRICT JSON only:
     { "recommendation":"bid|no_bid|conditional", "confidence":"high|medium|low", "headline":"...", "rationale":"2-4 sentences",
       "key_risks":[{"risk":"...","severity":"high|medium|low","detail":"..."}],   // PRIORITY output
       "deadline":{"date":"...","note":"..."}, "contract_value":{"amount":"... or null","note":"..."},
       "mandatory_requirements":["..."], "scoring":{"summary":"price vs quality split if stated","criteria":[{"criterion":"...","weight":"... or null"}]},
       "scope_summary":"3-5 sentences plain English", "notable_conditions":["bonds/retention/PCG/TUPE/unusual terms"], "missing_or_unclear":["clarify with buyer"] }
   - Validate against schema (pydantic). Malformed JSON → retry once stricter; still bad → fail cleanly, store no broken brief. Brief LEADS with recommendation + key_risks.

================================================================
PART C — BRIEF STORAGE + API
================================================================
7. Migration (next number): table tender_briefs — id, tender_id (FK CASCADE), status (generating|complete|failed), recommendation, confidence, headline, brief_json (jsonb), model, documents_considered (jsonb [{filename, included}]), input_tokens, output_tokens, error_detail, generated_at, created_at, updated_at; INDEX (tender_id, created_at desc). ORM + schemas.

8. API (mirror the fetch async-task pattern):
   - POST /tenders/{id}/generate-brief → 202 + id; background: ensure_content_extracted → assemble → LLM → validate → store. No documents → clear state "No documents to analyse — fetch documents first."
   - GET /tenders/{id}/brief → latest brief (status + brief_json when complete).
   - Regenerate = another POST (keep history; GET returns latest).

================================================================
PART D — DASHBOARD
================================================================
9. Wire the EXISTING "Generate brief" panel on /tenders/[id] → POST then poll GET:
   - generating: spinner "Reading the documents and analysing…".
   - complete: prominent recommendation badge (bid=mint, no_bid=red, conditional=amber) + confidence, headline, then KEY RISKS first (severity chips), then deadline, value, mandatory requirements (checklist), scoring split, scope summary, notable conditions, things-to-clarify. Elevated Genera theme. ~2 pages, scannable.
   - failed: clear error + Retry. no docs: steer to Fetch documents.
   - Footer: "Based on N of M documents (X truncated) · model · generated {time}".
   - Optional: on the documents panel, show a small "content stored" indicator so it's clear content is reusable without re-download.

================================================================
PART E — TESTS (mocked LLM + sample files; no API calls, no cost)
================================================================
- extractor: docx, multi-sheet xlsx, pdf (text + no-text), zip recursion, unsupported, corrupt → correct status; never throws.
- content store REUSE: same sha256 already extracted → reused, file NOT re-read; same sha256 on a second tender → content reused across tenders, not re-extracted; changed extractor_version → re-extracts. (This is the core "don't re-download/re-extract" guarantee — test it explicitly.)
- token budgeting: oversized set → prioritises ITT/quality/spec, truncates spreadsheets, records included/truncated/omitted.
- brief_generator with FAKE LLM (canned valid JSON) → validates, stores tender_briefs row, documents_considered populated.
- malformed JSON → retry → success; junk twice → clean failure, no broken brief.
- missing API key → clear error.
- API: no-docs state; with-docs 202→complete; GET latest; regenerate adds a record.
- All existing tests stay green. 25+ new tests.

================================================================
VERIFICATION
================================================================
- pytest green; ruff clean; dashboard tsc --noEmit + npm run build clean.
- NO real Anthropic call in the run (mocked). PR documents Friday live-test: ensure ANTHROPIC_API_KEY in backend .env; on tender 3169 (docs already fetched) click Generate brief → bid/no-bid brief leading with risks, from the 22 ITT docs; re-running uses STORED content (no re-download) — show the content_extracted reused=N log. Note rough cost per brief.

================================================================
SHIP — OPEN PR, DO NOT MERGE
================================================================
Commits:
- feat(db): tender_document_content store (extract once, reuse by sha256 across tenders) + migration
- feat(brief): document text extraction (docx/xlsx/pdf/zip) with per-file status
- feat(fetch): extract+store content at fetch time; reuse without re-download
- feat(brief): Anthropic LLM client (env key, mockable) + token budgeting
- feat(brief): generator → validated JSON, bid/no-bid + risks
- feat(db): tender_briefs + migration; feat(api): generate-brief + get-brief
- feat(dashboard): recommendation-led, risks-first brief panel (Elevated Genera)
- test: extractor + content-reuse + budgeting + generator + malformed-JSON + API (mocked LLM)

PR title: "feat: Phase 4 Chunk 5 — document content store + bid-brief engine (LLM, bid/no-bid + risks)"
Description: explain the content store (content becomes reusable DB data, fetched/extracted once by sha256, reused across tenders without re-downloading — the foundation for a future cloud DB + cross-tender comparison), the brief engine (stored content → Claude → 2-page recommendation-led brief), the JSON schema, that it's fully mocked (no real API call ran), the Friday live-test steps + cost ballpark. State at top: "DO NOT MERGE until Friday live-test — user is away." Sentinel: "Phase 4 Chunk 5 — content store + brief engine. Ready for review, hold merge for Friday."

RULES
- Single PR against main, OPEN, do NOT merge. Build from current main.
- Do NOT delete document files (separate later decision). Do NOT change Delta/bridge/fetch DOWNLOAD behaviour — only ADD content extraction/storage after persistence.
- LLM client MUST be mockable; ZERO real API calls in CI; never hardcode the key (env only).
- Never auto-make the bid decision or auto-submit — the brief recommends, the human decides.
- Reuse existing async-task + storage patterns; don't fork infrastructure. If blocked, "BLOCKED:" at top.

Begin.
