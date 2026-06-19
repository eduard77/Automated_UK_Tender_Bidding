# Document Fetch & AI-Processing Cost Audit

**VERDICT:** The cost is not in fetching documents — it is the one Sonnet call that runs **automatically over document text for every filter-matched tender** (`requirements_extractor.py`, default `claude-sonnet-4-5`), and the single cheapest effective lever is to make that extraction **on-demand** (run it only when a human opens or registers interest in a tender) instead of pre-emptively for everything that matches a saved filter.

> Read-only audit. No code, behaviour, or data was changed. Every claim is cited as `file:line` against `tender-agent/src/tender_agent/` unless noted. Pricing is per the Anthropic public price list (Sonnet 4.5/4.6 = $3 / $15 per 1M input/output tokens; Haiku 4.5 = $1 / $5; Message Batches API = 50% off). Token figures are order-of-magnitude estimates derived from the code's character caps, not measured bills.

---

## 1. Per-source: how documents are discovered and downloaded

There are **two separate document pipelines**, split by source type.

### Pipeline A — automatic, listing/feed adapters (`adapters/`)

Adapters registered in `adapters/__init__.py:10-18`. They normalise each source's notice and may attach a `documents[]` URL list; a background worker later downloads those URLs via `document_downloader.download_documents_for_tender` (`document_downloader.py:101-154`).

| Source | Docs discovered? | How | Typical / worst doc count |
|---|---|---|---|
| **Find a Tender (FTS)** | Yes — OCDS `tender.documents[]` | `services/normaliser.py:126-136` | 0–10+ URLs; **no per-tender cap** in Pipeline A (`document_downloader.py:107`) |
| **Contracts Finder (CF)** | Sometimes — OCDS URLs are usually HTML notice links, not files | normaliser + `portals/contracts_finder.py:11-17` | Often **0** real files |
| **Public Contracts Scotland (PCS)** | Yes — OCDS `documents[]` | `adapters/pcs.py:120-127` | as OCDS, no cap |
| **Sell2Wales (S2W)** | Yes — OCDS `documents[]` | `adapters/sell2wales.py:93-100` | as OCDS, no cap |
| **eTendersNI** | **No** — `documents=[]` hard-coded | `adapters/etendersni.py:103` | **0** always |
| **EU-Supply** | **No** — listing scrape only | `adapters/eu_supply.py:265` | **0** |
| **Atamis** | **No** — Visualforce listing scrape only | `adapters/atamis.py:296` | **0** |

**Cost-relevant:** the automatic path has **no document-count cap** — only a 50 MB per-file size cap (`config.py:139`, enforced `document_downloader.py:58-59`). A FTS/PCS/S2W tender with many `documents[]` URLs has all of them downloaded.

### Pipeline B — on-demand, portal adapters (`services/portals/adapters/`)

Run only when a human triggers `POST /tenders/{id}/fetch-documents` (`api/tender_fetch.py:131-155`).

| Portal | How docs are found / downloaded | Doc count |
|---|---|---|
| **Delta eSourcing** | Stage-One documents table parsed from the rendered DOM after Register Interest + login; each row downloaded via an individual browser click (`portals/adapters/delta_esourcing.py:312-332,1197-1351`) | **The heavy source** — live recon notes a 22-row table; hard cap **`MAX_DOCS = 50`**, 100 MB/file (`delta_esourcing.py:398-399`) |
| **Proactis / ProContract** | Activity-documentation + T&C lists parsed from DOM, in-session GET then click fallback (`portals/adapters/proactis.py:426-442,924-953`) | Handful typical; cap 50 / 100 MB (`proactis.py:311-312`) |
| **CF-direct / Fallback** | Public HTTP GET of allow-listed asset URLs (`portals/contracts_finder.py`, `portals/fallback.py`) | CF-direct cap 50 / 100 MB; Fallback 50 MB/file |

---

## 2. What happens to each downloaded document

### Storage (small cost)

- **Pipeline A & B → local disk only**, sha-sharded under `{document_storage_dir}/{tender_id}/...`; `storage_backend` is the literal `"local"` (`document_downloader.py:134-139`; `portal_orchestrator.py:942`). It does **not** write to Azure Blob.
- **Azure Blob is used by exactly one path:** the Delta local-fetch ingest endpoint `POST /tenders/{id}/ingest-documents` → `services/portals/document_ingest.py:164-204` via `get_storage_backend()`. (Matches the "Delta local-fetch pivot" design: fetch runs locally, bytes pushed to cloud DB + Blob.)

### Text extraction (small cost, no AI)

- Pipeline A: inline `pypdf` (PDF) / `python-docx` (docx), stored on `TenderDocumentFile.text_extracted` (`document_downloader.py:63-98,142`).
- Pipeline B: richer `pdfplumber→pypdf` + `python-docx` + `openpyxl` + zip recursion (`services/brief/document_extractor.py`), stored once per sha in `TenderDocumentContent` (`brief/content_store.py:94-191`).

### AI calls over the content — which model, when

The hand-off from "stored text" to "AI" is `aggregate_text(document_files)` (`document_downloader.py:157`), called by `extract_requirements` (`requirements_extractor.py:116`).

| AI feature | Model (default) | Input | When it runs |
|---|---|---|---|
| **Sector classification** | **Haiku** `claude-haiku-4-5-20251001` (`config.py:199`) | **Metadata only** (title + description + buyer), ≤512 out tokens; never reads documents | **Automatic, EVERY tender** — scheduler classification batch (`classifier.py:173`, `scheduler.py:130-148`) |
| **Requirements extraction** | **Sonnet** `claude-sonnet-4-5` (`config.py:190`) | **DOCUMENT CONTENT** — `aggregate_text()` capped at **80,000 chars ≈ 20k tokens** (`document_downloader.py:157`); falls back to description if no docs; `max_tokens=4096` (`requirements_extractor.py:126-131`) | **Automatic, every FILTER-MATCHED tender** — enrichment worker (`enrichment_worker.py:43-105`, `scheduler.py:485-495`); also on-demand `POST /admin/extract-requirements/{id}` |
| Bid-brief generation | **Sonnet** `claude-sonnet-4-6` (`brief/llm_client.py:25`) | **DOCUMENT CONTENT, largest** — up to `BRIEF_MAX_INPUT_TOKENS = 150,000` (`config.py`, `brief_generator.py:360-369,448`) | **On-demand only** — `POST /tenders/{id}/generate-brief`, auth + payment-gated (`api/tender_brief.py:68-147`) |
| Submission drafting | **Sonnet** `claude-sonnet-4-6` | Brief summary + ITT excerpts (3 docs × 2k chars) + vault evidence; budget 80k tokens; `max_tokens=8000` | **On-demand only**, per question (`submission/engine.py:251`) |
| Portal classification | **Sonnet** `claude-sonnet-4-5` | Portal homepage HTML, 5k chars (not tender docs) | Automatic per newly-discovered portal, rate-limited 1/3s (`portal_classifier.py:149`) |
| Inbound-email draft | **Sonnet** `claude-sonnet-4-6` | Email body ≤6k chars (metadata, not docs) | Automatic but **disabled by default** (`email_poll_enabled=false`) |
| Go/No-Go | **none** | — | Consumes the already-generated brief; no LLM call (`go_no_go/engine.py`) |
| Vault embeddings | **none** | — | Only `NullEmbedder`/`HashEmbedder` wired; no API spend |

**Key distinction:** only **requirements extraction (auto, matched)**, **brief (on-demand)** and **submission (on-demand)** run over actual document content. Classification — the only thing that runs over *every* tender — is deliberately Haiku and metadata-only (`config.py:196` comment: "do NOT switch this to Opus/Sonnet").

---

## 3. The two cost types, kept separate

### (a) Fetch + storage — small

Download bandwidth (≤50 MB/file cap) and local-disk/Blob storage. No model tokens. This is a few pence of egress/storage per tender at most and is **not** where the money goes.

### (b) AI processing — the cost that matters

Two calls run automatically; everything else is human-triggered.

**Classification (Haiku, every tender):** ~300 input + ~100 output tokens → ≈ **$0.0008 / tender** (`docs/build-progress.md:745-747` independently states sub-£0.001). Across a 50,000-tender corpus ≈ **$40 total**. Negligible.

**Requirements extraction (Sonnet 4.5, every matched tender)** — the cost driver:

- Worst case (tender with documents): ~20,000 input tokens × $3/1M = **$0.060** + ~2,000 output tokens × $15/1M = **$0.030** ≈ **$0.09 / matched tender**.
- Typical (smaller docs, ~8k input + ~1.5k output) ≈ **$0.045 / matched tender**.
- It runs once per matched tender (idempotent via the unique `TenderRequirements` row), so cost = `matched_tenders × ~$0.05`. At 5,000 matched tenders ≈ **$250**; at 20,000 ≈ **$1,000**. The total scales entirely with how broad the saved filter profiles are — broad filters mean most of the corpus gets a Sonnet document-content call.

**Brief (Sonnet 4.6, on-demand):** the heaviest *per call* — up to 150,000 input × $3/1M = **$0.45** + 4,096 output × $15/1M = $0.06 ≈ **$0.50 / brief** (×2 on JSON-retry). But it only fires when a human clicks Generate Brief and is payment-gated, so it is paid spend tied to demonstrated interest — not waste.

---

## 4. Where the past large Sonnet spend spike came from

**Specific path:** `requirements_extractor.py:126-127` — `client.messages.create(model=settings.anthropic_model, ...)` over `aggregate_text(...)`, default `claude-sonnet-4-5`.

**It did run automatically, for matched tenders.** Originally it ran **in-line inside the poll cycle** (`_enrich_matched_tender`, gated `if matched_profile_ids:`). `docs/build-progress.md:283-330` records the symptom: FTS emitted "a long unbroken series of `requirements.extracted` events… each ~10–15 seconds," consuming the entire poll interval and starving later sources.

**The fix was throttling, not a price cut.** Commit `6e95b71` (PR #124) *decoupled* the work into the out-of-band `enrichment_worker` (batch 5, every 5 min) — same Sonnet model, same per-matched-tender scope. There is **no commit that downgraded this path to Haiku or gated it behind human interest.** So Sonnet still runs automatically over document content for every matched tender today (`enrichment_worker.py:62-81`, `requirements_extractor.py:127`) — flagged here as the live cost.

---

## 5. Recommendation — cheapest effective lever

Three options, plainest first:

| Option | What it saves | Value lost |
|---|---|---|
| **Fetch fewer documents** | Almost nothing — fetch/storage is the cheap half (§3a). Doc *count* caps already exist on portals. | Low spend impact; not worth it. |
| **Fetch all, PROCESS fewer** (e.g. only the main document) | Cuts the Sonnet input from ~20k → a few k tokens, maybe 50–70% off each extraction. | Still pays a Sonnet call for every matched tender no human may ever open; misses requirements buried in secondary docs. |
| **Process on-demand only** — run requirements extraction (and any doc-content AI) **only when a human opens / registers interest** | **The most**: eliminates the Sonnet document-content call for every matched-but-never-opened tender. If only a small fraction of matched tenders are ever opened, this removes the large majority of recurring AI spend. | Near-zero — the brief/submission paths are *already* on-demand and payment-gated; this just aligns enrichment with the same principle. First open pays a one-time ~10–15s extraction latency. |

**Recommended lever: process on-demand only.** The product motto is "machines act, humans interpret"; today the most expensive AI call (Sonnet over full document text) is performed *before any human has shown interest*, for every tender that merely matches a saved filter — which is precisely the spend most likely to be wasted. Concretely: stop running `enrich_tender`/`extract_requirements` from the always-on `enrichment_worker` for all matched tenders, and instead trigger it lazily on first human view / register-interest (the on-demand endpoint `POST /admin/extract-requirements/{id}` already exists — `admin.py:46-97`). Keep classification on Haiku (it is already the cheap, deliberate "every tender" path). If some always-on enrichment is still wanted for a *small, high-signal* subset, gate it to that subset rather than the full match set, and/or switch the requirements model from Sonnet 4.5 to Haiku 4.5 (a further ~3× input / ~3× output unit-price cut) as a secondary lever.
