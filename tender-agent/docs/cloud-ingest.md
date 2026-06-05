# Cloud ingest (DB + Blob) — local-fetch pivot, PR 1

Delta refuses cloud/datacenter requests (confirmed 403 — the session is bound to
the operator's residential login IP). So Delta documents are fetched on the
**operator's own machine** (PR 2, `scripts/fetch_delta.py`) and pushed **up** to
this backend, which is the system of record. PR 1 is the cloud side that receives
them: an authenticated ingest endpoint that writes the DB rows and stores the
original bytes in **Azure Blob** via **managed identity**.

## What PR 1 adds

- **`POST /tenders/{id}/ingest-documents`** (`require_account`) — multipart:
  - `metadata`: a JSON string, a list of per-document objects
    (`title`, `format`, `content_type`, `sha256`, `extracted_text`, `char_count`,
    `extraction_status`, `extraction_detail`, `doc_type`, `extractor_version`).
  - `files`: the document byte parts, **in the same order** as `metadata`.
  - Returns per-file outcomes + dedup counts. The `sha256` is **recomputed
    server-side** (the client value is only a cross-check).
- **Azure Blob storage backend** (`services/storage/`) — `azure_blob` alongside
  the existing `local`. Layout: `{tender_id}/{sha256}.{ext}` in container
  `tender-documents`. Auth is `DefaultAzureCredential` (managed identity in
  Azure, dev creds locally) — **no connection string**.
- **Idempotent ingest** (`services/portals/document_ingest.py`) — a parallel
  persistence path (the orchestrator's `_persist_documents` reads local disk and
  hard-codes `storage_backend="local"`, so it can't be reused). Dedup by
  `(tender_id, sha256)`; content rows reuse `(sha256, extractor_version)`.
  Re-ingesting the same documents writes no duplicate rows and skips re-upload.
- **Cloud no longer drives Delta.** The orchestrator returns `needs_local_fetch`
  for platforms in `local_fetch_platforms` (default `["delta_esourcing"]`) unless
  the process is the local runner (`LOCAL_FETCH_RUNNER=true`). This avoids
  contending with the operator's local Delta session. The Delta session
  upload/status/test endpoints are unchanged.

## Backend environment variables

| Env var | Value | Notes |
|---|---|---|
| `AZURE_STORAGE_ACCOUNT` | `generasystemsfiles` | Empty → falls back to local-disk backend (CI/dev need no Azure). |
| `AZURE_BLOB_CONTAINER` | `tender-documents` | Default already correct; set explicitly for clarity. |
| `INGEST_MAX_BYTES` | (optional) | Per-document cap; default 100 MB (matches the Delta adapter). |
| `LOCAL_FETCH_RUNNER` | unset / `false` on the cloud | The local CLI (PR 2) sets `true` in its own process. |

### Managed identity (already configured)

- GeneraTender App Service has a **system-assigned managed identity**
  (object id `2d19558b-2f8f-4227-b602-86238f1c49e7`).
- It is granted **Storage Blob Data Contributor** on storage account
  `generasystemsfiles`.
- `DefaultAzureCredential` picks this up automatically in Azure; no secret or
  connection string is stored anywhere.

## Deploy steps (required — backend Python + new dependencies changed)

PR 1 adds `azure-storage-blob` and `azure-identity` to `pyproject.toml`, so the
image **must be rebuilt** (a Stop/Start alone won't pick up new deps or code):

1. Merge this PR to `main`.
2. **Rebuild the image** from current `main`:
   `az acr build` against `tender-agent/Dockerfile` (build context `tender-agent`).
3. **Set the App Service application settings**:
   `AZURE_STORAGE_ACCOUNT=generasystemsfiles`, `AZURE_BLOB_CONTAINER=tender-documents`.
   Leave `LOCAL_FETCH_RUNNER` unset on the cloud.
4. **Stop/Start** the App Service so it pulls the new image and reads the new
   settings.
5. Verify: `POST /tenders/{id}/ingest-documents` (authenticated, with a tiny test
   file) returns `inserted: 1` and the blob appears under
   `tender-documents/{tender_id}/{sha256}.{ext}`. A Delta fetch via the cloud
   orchestrator now returns `needs_local_fetch` rather than driving the browser.

## What's next (not in PR 1)

- **PR 2**: `scripts/fetch_delta.py` — local headed fetch that runs the existing
  orchestrator with `LOCAL_FETCH_RUNNER=true`, then uploads to this endpoint;
  plus the dashboard-gated Register-Interest confirm coordination.
- **PR 3**: dashboard UI for the Register-Interest confirm button.
