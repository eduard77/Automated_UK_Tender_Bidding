# Automated UK Tender Bidding

A UK public-sector tender discovery and bid-automation system. Monitors all major UK
tender sources, analyses tender requirements with Claude, matches them against a
document vault, drafts responses, and prepares bids for human review and submission.

> **Submission is always human-gated.** This is decision-support and execution
> assistance, not a fully autonomous bidder. See [`tender-agent/PROJECT.md`](tender-agent/PROJECT.md) §7.

## Project status

| Phase | Status | Description |
|---|---|---|
| 1 | ✅ Done | Discovery service — 5 UK source adapters (FTS, CF, PCS, S2W, NI), OCDS normaliser, dedup, filter engine, scheduler, REST API |
| 2 | ✅ Done | All 5 adapters with fixture tests, document downloader, Claude requirements extractor + validation harness, dashboard PWA (list / filters / detail + push), Web Push end-to-end |
| 3 | ⬜ Designed | Document vault with claims records and per-tender re-validation |
| 4 | ⬜ Designed | First portal adapter (Playwright) |
| 5 | ⬜ Designed | Vault-grounded drafting agent + case study generator |
| 6 | ⬜ Designed | Email integration + Temporal workflows + notifications |
| 7 | ⬜ — | Hardening, observability, additional portals |

> **T6 (AWS Terraform deploy)** is parked until deployment decisions are made:
> AWS account access, dashboard domain name, and budget approval. When those
> are confirmed, T6 is the next task.

Full design in [`tender-agent/PROJECT.md`](tender-agent/PROJECT.md). AI agent
conventions in [`tender-agent/CLAUDE.md`](tender-agent/CLAUDE.md).

## What's verified in development

This system has been built and exercised entirely in the development sandbox.
The pieces that are tested and the pieces that still need a live shake-down:

| Component | Verified in dev | Notes |
|---|---|---|
| Five adapters (FTS, CF, PCS, S2W, NI) | ✅ unit-tested against fixtures | No live UK API run yet — Codespaces egress is firewalled to those endpoints. |
| OCDS normaliser, deduplicator, filter engine | ✅ unit-tested | Phase 1 work, regression-stable. |
| Postgres schema + migrations | ✅ `alembic upgrade head` on 0001-0003 | Run on a fresh native Postgres 16 cluster. |
| FastAPI endpoints (tenders / filters / push / admin) | ✅ unit + curl smoke | Exercised end-to-end via `uvicorn` + `psql`. |
| Dashboard pages (`/`, `/filters`, `/tenders/[id]`) | ✅ `npm run build` clean + dev-server route smoke | All routes serve 200 against the live backend; SWR data paths verified by curl. |
| Web Push subscribe / dispatch / 410-handling | ✅ via `pywebpush` mocks + DB inspection | Real browser ↔ FCM/Mozilla delivery untested — sandbox can't reach push providers. |
| Requirements extractor | ✅ dry-run validation pass on seeded tenders | Live ≥10-tender validation pass deferred until live API egress is available. |

The first time this runs against the live UK APIs and a real browser, expect a
short triage cycle to handle real-world quirks. The structured-log events we
emit (`*.fetch_failed`, `push.send_*`, `requirements.parse_failed`, ...) are
the operator's primary signal.

## Repository layout

```
.
├── tender-agent/                Backend (Python · FastAPI · Postgres)
│   ├── src/tender_agent/
│   ├── tests/                   Unit + fixture tests (48 passing)
│   ├── alembic/versions/        0001 / 0002 / 0003 migrations
│   ├── scripts/                 Operator scripts (extractor validation, etc.)
│   ├── docker-compose.yml
│   ├── PROJECT.md               ← Design spec (start here)
│   ├── CLAUDE.md                ← Conventions & guardrails for AI agents
│   └── README.md                ← Backend quickstart + endpoint reference
├── tender-agent-dashboard/      Frontend (Next.js · Tailwind · PWA · Web Push)
│   └── README.md                ← Dashboard quickstart
├── docs/                        Cross-cutting operator docs
│   ├── push-setup.md            ← VAPID keypair + end-to-end push setup
│   └── extractor-validation.md  ← Extractor validation harness usage
├── .github/                     CI workflows, issue/PR templates, dependabot
└── README.md                    This file
```

## Quickstart

The complete walkthrough is in [`tender-agent/README.md`](tender-agent/README.md).
The 30-second version:

```bash
# Backend
cd tender-agent
cp .env.example .env             # at minimum set ANTHROPIC_API_KEY
docker compose up --build

# Dashboard (separate terminal)
cd tender-agent-dashboard
cp .env.example .env.local
npm install
npm run dev
```

- API: <http://localhost:8000> · OpenAPI: `/docs`
- Dashboard: <http://localhost:3000>

### Run tests

```bash
cd tender-agent
pip install -e ".[dev]"
pytest -v                        # 48 passing across services, adapters, push, admin
ruff check src/ tests/

cd ../tender-agent-dashboard
npm install
npx tsc --noEmit
npm run build
```

## Working with Claude Code

Claude Code reads [`tender-agent/CLAUDE.md`](tender-agent/CLAUDE.md) automatically
when run from this repo. The first session in a new clone should start by reading
both `PROJECT.md` and `CLAUDE.md`.

```bash
npm install -g @anthropic-ai/claude-code
cd Automated_UK_Tender_Bidding
claude
```

## License

Proprietary — internal use only. See [LICENSE](LICENSE).
