# Tender Agent — Pass 1 (Discovery Service)

UK public tender discovery and bid-automation agent. **This is Pass 1** of the build —
the core discovery service that polls UK tender sources, normalises them into a single
schema, deduplicates across sources, and matches against your filter profiles.

Pass 2 (next) adds: remaining UK source adapters (Scotland, Wales, NI), Claude-powered
requirements extraction, dashboard UI with PWA + push notifications, and AWS deploy
config. Subsequent passes add the document vault, drafting agents, portal adapters,
and the full bid workflow.

## What's in Pass 1

- **Source adapters** for **Find a Tender Service** and **Contracts Finder** (covers most
  high-value UK public tenders by volume).
- **OCDS normaliser** — converts diverse source payloads into one canonical `Tender`
  schema (title, buyer, value, deadlines, CPV codes, documents, etc.).
- **Cross-source deduplicator** — same tender often appears on multiple sources;
  detected via procurement reference + fuzzy buyer/title/value matching.
- **Filter engine** — define profiles by CPV codes/prefixes, keywords (any/all/none),
  buyer, region, country, value range, notice type, minimum days to deadline.
- **Scheduler** — APScheduler runs polls at the configured interval and per-source.
- **REST API** — list/query tenders, manage filter profiles, trigger polls.
- **Postgres** schema with proper indexes, audit log of every poll.

## Quickstart (local)

Requirements: Docker + Docker Compose.

```bash
git clone <this-repo>
cd tender-agent
cp .env.example .env
docker compose up --build
```

The API is on `http://localhost:8000`, OpenAPI docs at `/docs`.

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

### Trigger a poll immediately (instead of waiting for the scheduler)

```bash
curl -X POST http://localhost:8000/admin/poll-now
```

### List matched tenders

```bash
curl "http://localhost:8000/tenders?matched_only=true&limit=20" | jq
```

## Local development (without Docker)

```bash
# Postgres running locally on 5432 with user/db `tender`
pip install -e ".[dev]"
alembic upgrade head
uvicorn tender_agent.main:app --reload
```

Run tests:

```bash
pytest -v
```

## Project layout

```
src/tender_agent/
├── main.py              FastAPI entrypoint + lifespan
├── config.py            Settings (env-driven)
├── db.py                SQLAlchemy session
├── models.py            ORM models — Tender, Source, FilterProfile, FilterMatch, PollRun
├── schemas.py           Pydantic schemas (API + internal)
├── scheduler.py         APScheduler job: poll all sources at interval
├── adapters/            One module per tender source
│   ├── base.py          Abstract SourceAdapter
│   ├── fts.py           Find a Tender Service (OCDS)
│   └── contracts_finder.py
├── services/
│   ├── normaliser.py    OCDS release -> NormalisedTender
│   ├── deduplicator.py  Cross-source dedupe
│   ├── filter_engine.py Match tenders against filter profiles
│   └── ingestion.py     Orchestrates poll-and-ingest cycle
└── api/
    ├── tenders.py       GET /tenders, GET /tenders/{id}
    └── filters.py       CRUD /filters, POST /admin/poll-now
alembic/                 DB migrations
tests/                   Unit tests (10 passing)
```

## How it works

1. On startup, `scheduler.ensure_sources()` makes sure each registered adapter has a
   `Source` row.
2. APScheduler runs `poll_all` every `POLL_INTERVAL_MINUTES` (default 30).
3. For each enabled source, `poll_source` invokes the adapter's `fetch_since(last_polled_at)`.
4. Each yielded `NormalisedTender` is upserted via `_upsert_tender`, which:
   - Finds existing record by `(source_code, source_ref)` — updates if changed.
   - For new records, runs the deduplicator to link cross-source duplicates.
   - Computes a content hash so we only mark "updated" when something material changed.
5. New/updated tenders are matched against all enabled `FilterProfile` records;
   matches stored in `FilterMatch` (unique per tender × profile).
6. The audit `PollRun` row records: started/finished, status, fetched/new/updated counts, errors.

## Notes on the source APIs

Both FTS and Contracts Finder publish in **OCDS** (Open Contracting Data Standard),
which is why they share the normaliser. OCDS variations between sources are handled in
`ocds_release_to_tender`. If a source updates its API shape, the normaliser is the
single place to adjust.

The adapters expect HATEOAS-style `links.next` cursor pagination. If a source returns
no `next` link, paging stops.

If the live API endpoints differ slightly from the documented shape (path names,
parameter names), override the base URL in `.env`:

```
FTS_API_BASE=https://www.find-tender.service.gov.uk/api/1.0
CONTRACTS_FINDER_API_BASE=https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS
```

## What's coming in Pass 2

- Adapters for Public Contracts Scotland, Sell2Wales, eTendersNI
- Document downloader: pre-fetches the ITT/spec PDFs for matched tenders
- Claude-powered requirements pre-extractor: produces a structured requirements list
  and one-page brief per matched tender
- Web dashboard (Next.js, installable as PWA, with web push notifications)
- AWS deploy: ECS Fargate + RDS Postgres + S3 (for documents) + Secrets Manager
- Step-by-step deploy README

After Pass 2, the discovery service will be production-ready and you'll have a working
PWA on your phone receiving push alerts for matched tenders, each with a structured
brief generated by Claude. From there we move to the document vault (Pass 3).

## License

Internal — not yet licensed for redistribution.
