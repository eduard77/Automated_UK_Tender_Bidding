Should show real PDF/DOCX files matching the count.
    f. Verify GET /tenders/{tender_id}/documents/{doc_id}/file returns real document bytes (curl with -o to save and verify file opens).
    g. Dashboard: navigate to /tenders/{id}, see the documents section populated.

15. Try a tender WITHOUT documents:
    a. Find a tender where documents array is empty.
    b. Call fetch-documents.
    c. Verify: status='complete' but files list is empty; UI shows appropriate empty state ("No documents available for this tender from CF").

16. Try a tender on a non-CF platform (e.g. one from PCS or FTS):
    a. Verify the orchestrator picks FallbackAdapter, returns partial result.
    b. UI shows "Limited documents available — full documents require login to {platform}".

17. CI gates:
    - pytest -v → 109 existing + 20 new = 129+ tests pass
    - ruff check src/ tests/ clean
    - npx tsc --noEmit clean
    - npm run build clean

18. Screenshots:
    - /tenders/{id} with documents section populated post-fetch
    - /tenders/{id} with fetch in progress (capture during the actual run if you can; otherwise mock state)
    - /tenders/{id} for a non-CF tender showing fallback messaging
    Save to docs/screenshots/.

================================================================
PART G — SHIP
================================================================

19. Commit chunks:
    - feat(db): tender_documents + fetch_tasks tables
    - feat(adapters): ContractsFinderDirectAdapter + registry wiring
    - feat(services): real fetch_tender_documents orchestration + task queue
    - feat(api): real fetch-documents endpoint + document file serving
    - feat(dashboard): documents panel on tender detail page
    - test: cf adapter + orchestrator + task + dedup + serving tests
    - docs: screenshots

20. PR title: "feat: Phase 4 Chunk 3 — First real adapter (Contracts Finder direct)"

    Description must include:
    - Summary of what's in
    - The path from "click Fetch documents" to "files on disk" — short flow diagram or step list
    - Smoke test results: which tender ID you tested, how many documents fetched, where they live on disk
    - 3 dashboard screenshots embedded
    - One paragraph honestly explaining what's NOT in this prompt: no auth, no Playwright (CF adapter uses plain httpx because the documents are public). That comes in chunk 4 onward.
    - The sentinel "Phase 4 Chunk 3 — Contracts Finder direct adapter complete. Ready for review." at the end

================================================================
RULES YOU MUST FOLLOW
================================================================

- Single PR. No intermediate PRs.
- Don't try to fetch from any portal other than assets.publishing.service.gov.uk in this PR. CF adapter is intentionally narrow.
- Untrusted URLs (anything not matching the platform's domain_patterns) MUST be rejected by the adapter, never fetched. Security boundary.
- Files saved to disk MUST have sanitised filenames — strip path components, illegal characters, leading dots, etc. Use werkzeug.utils.secure_filename or equivalent.
- Files MUST be size-capped at 100 MB per document. Reject larger.
- Total documents per tender capped at 50. Reject more.
- The fetch_tender_documents endpoint must be idempotent: calling twice on the same tender doesn't duplicate documents (sha256 dedup).
- No new top-level dependencies beyond httpx (which is already used elsewhere). 
- All tests use mocked httpx responses. No real network in CI.
- Do NOT stop and ask user. They're at work. Document blockers with "BLOCKED:" at top of PR.

================================================================
STOP CRITERIA
================================================================

Stop only when ALL of these are true:
- PR open against main, marked ready for review
- CI green
- 129+ tests pass
- ruff + tsc + npm build clean
- Smoke test in Part F actually run against a real CF tender, files actually fetched and visible on disk
- 3 dashboard screenshots in PR description
- Sentinel line in PR description: "Phase 4 Chunk 3 — Contracts Finder direct adapter complete. Ready for review."

If unresolvable: "BLOCKED:" at top of PR description, push branch, mark draft.

Begin.
