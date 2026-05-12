# Automated UK Tender Bidding

A UK public-sector tender discovery and bid-automation system. Monitors all major UK
tender sources, analyses tender requirements with Claude, matches them against a
document vault, drafts responses, and prepares bids for human review and submission.

> **Submission is always human-gated.** This is decision-support and execution
> assistance, not a fully autonomous bidder. See `tender-agent/PROJECT.md` §7.

## Project status

| Phase | Status | Description |
|---|---|---|
| 1 | ✅ Done | Discovery service — 5 UK source adapters, OCDS normaliser, dedup, filter engine, scheduler, REST API |
| 2 | ⏳ ~70% | Document downloader, Claude requirements extractor, dashboard PWA (scaffold), AWS deploy |
| 3 | ⬜ Designed | Document vault with claims records and per-tender re-validation |
| 4 | ⬜ Designed | First portal adapter (Playwright) |
| 5 | ⬜ Designed | Vault-grounded drafting agent + case study generator |
| 6 | ⬜ Designed | Email integration + Temporal workflows + notifications |
| 7 | ⬜ — | Hardening, observability, additional portals |

Full design in [`tender-agent/PROJECT.md`](tender-agent/PROJECT.md). AI agent
conventions in [`tender-agent/CLAUDE.md`](tender-agent/CLAUDE.md).

## Repository layout

```
.
├── tender-agent/                Backend (Python · FastAPI · Postgres)
│   ├── src/tender_agent/
│   ├── tests/
│   ├── alembic/
│   ├── docker-compose.yml
│   ├── PROJECT.md               ← Design spec (start here)
│   ├── CLAUDE.md                ← Conventions & guardrails for AI agents
│   └── README.md                ← Backend quickstart
├── tender-agent-dashboard/      Frontend (Next.js · Tailwind · PWA)
├── .github/                     CI workflows, issue/PR templates
└── README.md                    This file
```

## Quickstart

### Run the backend locally

```bash
cd tender-agent
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY
docker compose up --build
```

API at <http://localhost:8000>, OpenAPI docs at `/docs`.

### Run the dashboard locally

```bash
cd tender-agent-dashboard
npm install
npm run dev
```

Dashboard at <http://localhost:3000>.

### Run tests

```bash
cd tender-agent
pip install -e ".[dev]"
pytest -v
ruff check src/ tests/
```

## Working with Claude Code

Claude Code reads `tender-agent/CLAUDE.md` automatically when run from this repo. The
first session should always start by reading both `PROJECT.md` and `CLAUDE.md`.

```bash
npm install -g @anthropic-ai/claude-code
cd Automated_UK_Tender_Bidding
claude
```

## License

Proprietary — internal use only. See [LICENSE](LICENSE).
