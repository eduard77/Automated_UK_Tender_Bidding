# Tender Agent — Backend

UK public tender discovery + bid-automation agent. Polls the five major UK
tender sources, normalises them into one schema, deduplicates across sources,
matches against operator filter profiles, extracts a structured requirements
brief via Claude, and dispatches Web Push notifications when filters match.

Submission is **always** human-gated. See [`PROJECT.md`](PROJECT.md) §7.

## What's in the backend today

After Phase 2:

- **Five source adapters** — Find a Tender (FTS), Contracts Finder (CF), Public
  Contracts Scotland (PCS), Sell2Wales (S2W), eTendersNI (NI). Each has a
  fixture-based test suite (`tests/test_adapter_{code}.py`).
- **OCDS normaliser** — converts FTS/CF/PCS/S2W payloads into one canonical
  `Tender`. NI's Atom feed has its own normalisation inline.
- **Cross-source deduplicator** — same tender often appears on multiple sources;
  detected via procurement reference + fuzzy buyer/title/value matching.
- **Filter engine** — define profiles by CPV codes/prefixes, keywords
  (any/all/none), buyer, region, country, value range, notice type, minimum
  days to deadline.
- **Document downloader** — pre-fetches ITT/spec attachments for matched
  tenders. Streams to local disk (S3 in prod), tracks size + sha256 + format.
- **Requirements extractor** — Claude reads aggregated document text + tender
  metadata, returns a strict JSON brief: summary, evaluation criteria,
  mandatory + desired requirements, documents required, questions to answer,
  risk flags, recommendation (pursue / decline / review).
- **Web Push** — `push_subscriptions` table + dispatch via `pywebpush`; the
  ingestion hook fires one notification per new `FilterMatch`. See
  [`docs/push-setup.md`](../docs/push-setup.md).
- **Validation harness** — `scripts/validate_extractor.py` runs the extractor
  over real tenders, checks schema conformance + requirement grounding +
  number grounding, produces a markdown report. See
  [`docs/extractor-validation.md`](../docs/extractor-validation.md).
- **Scheduler** — APScheduler runs polls at the configured interval.
- **REST API + admin endpoints** — see [Endpoints](#endpoints) below.
- **Postgres** schema with proper indexes; audit log of every poll
  (`poll_runs`).

## Quickstart (Docker)

```bash
cd tender-agent
cp .env.example .env
# At minimum, set ANTHROPIC_API_KEY. For push, also set VAPID_*.
docker compose up --build
```

API: <http://localhost:8000> · OpenAPI docs: `/docs`

### Create a filter profile

```bash
curl -X POST http://localhost:8000/filters \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Cleaning services South West",
    "cpv_prefixes": ["909"],
    "regions": ["South West"],
    "value_min": 50000,
    "min_days_to_deadline": 7
  }'
```

### Trigger a poll immediately

```bash
curl -X POST http://localhost:8000/admin/poll-now
```

### List matched tenders

```bash
curl "http://localhost:8000/tenders?matched_only=true&limit=20" | jq
```

### Re-extract requirements for a tender

Useful if the extractor prompt has changed or the tender's documents were
re-downloaded after the original extraction.

```bash
curl -X POST http://localhost:8000/admin/extract-requirements/42
```

### Validate the extractor across N tenders

```bash
python scripts/validate_extractor.py --recent 10 --output report.md
```

See [`docs/extractor-validation.md`](../docs/extractor-validation.md) for the
full workflow, target thresholds, and what to do when it flags a hallucination.

## Local development (without Docker)

Requires Postgres 16 running locally with `tender` user + `tender_agent` DB.

```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn tender_agent.main:app --reload
```

Run tests:

```bash
pytest -v                                     # ~48 cases across services, adapters, push, admin
ruff check src/ tests/ scripts/
```

The fixture-based adapter tests run fully offline (`httpx.MockTransport` per
`tests/conftest.py`). No live UK API call ever happens in tests.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe. |
| GET | `/tenders` | List with filters: `source`, `buyer`, `status`, `matched_only`, `include_duplicates`, `limit`, `offset`. |
| GET | `/tenders/{id}` | One tender, full payload. |
| GET | `/tenders/{id}/requirements` | Extracted brief; 404 if not yet extracted. |
| GET | `/tenders/{id}/documents` | Downloaded files for the tender. |
| GET | `/filters` | List filter profiles (with `match_count`). |
| POST | `/filters` | Create a filter profile. |
| PATCH | `/filters/{id}` | Partial update (e.g. `{"enabled": false}` toggle). |
| PUT | `/filters/{id}` | Full replace using the create-shape body. |
| DELETE | `/filters/{id}` | Delete + cascade match history. |
| POST | `/push/subscriptions` | Browser push subscribe (anonymous, by endpoint). |
| DELETE | `/push/subscriptions` | Unsubscribe by endpoint. |
| GET | `/push/vapid-public-key` | VAPID public key for the dashboard. |
| POST | `/admin/poll-now` | Force a poll cycle across all enabled sources. |
| POST | `/admin/extract-requirements/{tender_id}` | Re-run extraction; overwrites any existing brief. |

## Project layout

```
src/tender_agent/
├── main.py                  FastAPI entrypoint + lifespan
├── config.py                Settings (env-driven)
├── db.py                    SQLAlchemy session
├── models.py                ORM models — Tender, Source, FilterProfile,
│                            FilterMatch, PollRun, TenderDocumentFile,
│                            TenderRequirements, PushSubscription
├── schemas.py               Pydantic schemas (API + internal)
├── scheduler.py             APScheduler job: poll all sources at interval
├── adapters/                One module per tender source (FTS / CF / PCS / S2W / NI)
│   └── base.py              Abstract SourceAdapter (testable via injected client)
├── services/
│   ├── normaliser.py        OCDS release -> NormalisedTender
│   ├── deduplicator.py      Cross-source dedupe
│   ├── filter_engine.py     Match tenders against filter profiles
│   ├── ingestion.py         Orchestrates poll → upsert → match → enrich → push
│   ├── document_downloader.py
│   ├── requirements_extractor.py
│   └── push.py              Web Push dispatch (pywebpush)
└── api/
    ├── tenders.py           /tenders, /tenders/{id}/...
    ├── filters.py           CRUD /filters + POST /admin/poll-now (legacy)
    ├── push.py              /push/subscriptions, /push/vapid-public-key
    └── admin.py             /admin/extract-requirements/{id}

alembic/versions/            0001 initial · 0002 documents+requirements · 0003 push_subscriptions
scripts/
└── validate_extractor.py    Extractor validation harness
tests/
├── conftest.py              Fixture helpers (httpx.MockTransport wrappers)
├── fixtures/                JSON + XML fixtures for adapter tests
├── test_adapter_{fts,cf,pcs,sell2wales,etendersni}.py
├── test_admin_extract.py    /admin/extract-requirements/* unit tests
├── test_filter_engine.py
├── test_normaliser.py
├── test_push.py
└── test_requirements_extractor.py
```

## How a poll cycle runs

1. `scheduler.ensure_sources()` makes sure each registered adapter has a `Source` row.
2. APScheduler runs `poll_all` every `POLL_INTERVAL_MINUTES` (default 30).
3. For each enabled source, `poll_source` invokes the adapter's
   `fetch_since(last_polled_at)`.
4. Each yielded `NormalisedTender` is upserted via `_upsert_tender`:
   - find existing by `(source_code, source_ref)`, update if `content_hash` changed;
   - for new records, run the deduplicator to link cross-source duplicates.
5. New / updated tenders are matched against enabled `FilterProfile` records;
   matches stored in `FilterMatch` (unique per tender × profile).
6. For each new match: download documents → extract requirements → dispatch
   push to subscribers of that profile (plus catch-all subscribers with
   `filter_profile_id IS NULL`).
7. The audit `PollRun` row records started/finished/status/fetched/new/updated.

## Notes on the source APIs

FTS / CF / PCS / S2W publish in OCDS, sharing
`services/normaliser.ocds_release_to_tender`. Per-source quirks (URL paths,
parameter names — kebab-case `updated-from` for FTS vs camelCase `updatedFrom`
for the others) live in each adapter; the normaliser is the single place to
adjust if OCDS shapes drift.

NI is Atom XML and has its own per-entry normalisation in `etendersni.py`. It's
also the only adapter that post-filters by date (the others delegate to the
upstream's `updatedFrom` query string).

If a source's live API shape differs from documented, override the base URL in
`.env` — every base URL is configurable.

## License

Internal — not yet licensed for redistribution.
