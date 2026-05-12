# CLAUDE.md — Operating instructions for Claude Code

This file tells **Claude Code** how to work on the Tender Agent project. It pairs
with `PROJECT.md` (the design spec). Read PROJECT.md first; this file covers
**how to work**, not what to build.

If you're a human reading this: feel free, but the audience is the AI agent.

---

## 1. Orientation

**Project**: UK public-sector tender discovery and bid-automation agent.

**Read first, in this order**:
1. `PROJECT.md` — full design, build phases, acceptance criteria. **Authoritative.**
2. `README.md` — quickstart and current state.
3. This file.
4. Existing code in `src/tender_agent/` to absorb conventions.

**Where things live**: §4 of PROJECT.md has the source tree. Don't re-derive — look it up.

**Status**: Phase 1 ✅ done. Phase 2 ✅ done (all 5 adapters with fixture tests,
document downloader, Claude requirements extractor + validation harness, dashboard
PWA, Web Push end-to-end). Phases 3-7 designed but not implemented.

**T6 (AWS Terraform deploy)** is parked until deployment decisions are made:
AWS account access, dashboard domain name, and budget approval. When those are
confirmed, T6 is the next task.

---

## 2. Operating principles

### Do design-then-code

When picking up a new component, the first step is to **read the spec in PROJECT.md
§5** for that component, then sketch the interfaces and types in a comment or a draft
file, then implement. If the spec is missing or ambiguous, **stop and surface the gap**
in your response rather than guessing.

### Don't re-litigate decisions already made

Stack, schema names, architecture patterns, and the human-checkpoint policy are
decided. If you think one is wrong, say so explicitly in chat — don't quietly do
something different.

### Small commits, working code

Prefer commits that ship one working slice over big WIP dumps. Each commit:
- Compiles / lints / passes existing tests.
- Has a focused message: `feat(vault): claims extractor for insurance docs`.

### Tests are not optional

Every new service function in `services/` ships with a unit test. Every new adapter
ships with a fixture-based test. No test → not done.

### Read-edit-write order matters

Before editing any file, **view it first** to confirm current state. After several
edits to the same file, view again — your earlier view is stale.

---

## 3. Conventions (the parts that bite you)

### Python

- Python 3.12, `from __future__ import annotations` at the top of every module.
- Type hints everywhere — including return types on private helpers.
- Ruff config in `pyproject.toml`; respect it. Line length 100.
- No business logic in FastAPI route handlers. Handlers call into `services/`.
- DB-touching service functions take `db: Session` (or `AsyncSession`) as the first
  argument. No hidden module-level sessions.
- Async-first for I/O. Adapters, downloaders, and Anthropic calls are async.
- `structlog` for logging. JSON in prod, never `print()`. Log shape:
  `logger.info("event_name", key1=val, key2=val)`.
- Pydantic for all I/O models. SQLAlchemy for DB models. Never mix the two —
  `from_attributes=True` in the read-schema config.

### TypeScript

- Strict mode on. `any` is banned — use `unknown` and narrow.
- App Router only. No Pages Router.
- Server components by default; `"use client"` only when interactivity is needed.
- Tailwind for styling. Custom design tokens in `tailwind.config.mjs`.
- API client in `lib/api.ts`. Don't `fetch()` directly from components — go through
  the typed helpers.

### SQL

- Every schema change is a new Alembic revision file under `alembic/versions/`,
  numbered sequentially (`0003_*.py`, `0004_*.py`, ...).
- Never edit a previously-merged migration.
- Always implement `downgrade()` — yes, even when it's tedious.
- Use Postgres-native types where appropriate: `JSONB` (not `JSON`), `ARRAY(String)`,
  `Numeric(18, 2)` for money.

### Secrets

- All secrets via env vars locally, Secrets Manager in AWS.
- `.env.example` documents every required key. **Update it** whenever you add a new
  one.
- Never commit `.env`. Never log secret values, ever — log a hash or a fingerprint
  if you must.
- Portal credentials are stored as Secrets Manager ARNs in the DB
  (`portal_credential_refs.secret_arn`), never as plaintext.

---

## 4. Common commands

```bash
# Backend
cd tender-agent
pip install -e ".[dev]"                       # one-time
alembic upgrade head                          # apply migrations
uvicorn tender_agent.main:app --reload        # run dev server
pytest -v                                     # tests (48 cases)
ruff check src/ tests/ scripts/               # lint
ruff check src/ tests/ scripts/ --fix         # auto-fix
alembic revision -m "add foo table"           # new migration

# Operator scripts
python scripts/validate_extractor.py --recent 10 --output report.md
python scripts/validate_extractor.py --tender-id 42 --dry-run

# Frontend
cd tender-agent-dashboard
npm install
npm run dev                                   # localhost:3000
npm run build && npm start                    # prod build
npx tsc --noEmit                              # type check (gated by CI)
npm run generate-vapid                        # generate VAPID keys for push

# Stack via Docker
docker compose up --build                     # full stack

# Database access
psql postgresql://tender:tender@localhost:5432/tender_agent
```

When running shell commands, use `bash_tool` from the project root unless explicit
about `cd`-ing.

---

## 5. Do / Don't

### Do

- ✅ Read PROJECT.md before designing anything new.
- ✅ Add a test alongside every new service function.
- ✅ Use `structlog` and add useful context to log events.
- ✅ Commit per logical unit — feature, fix, refactor, test, docs.
- ✅ Update `.env.example` and `README.md` when behaviour changes.
- ✅ Run `ruff check` and `pytest` before declaring something done.
- ✅ View files before editing them; re-view after multiple edits.
- ✅ Use `str_replace` for surgical edits, `create_file` for new files; don't
  `cat > file` from bash.
- ✅ When the spec is silent, **ask** rather than invent.

### Don't

- ❌ Don't add a new top-level dependency without saying why in your response.
- ❌ Don't put business logic in route handlers.
- ❌ Don't commit `.env`, `.aws/`, or anything that smells like a secret.
- ❌ Don't auto-submit anything to a buyer portal. Ever. The submission step is
  human-gated permanently — see §7 of PROJECT.md.
- ❌ Don't auto-send email to a buyer. Drafts only.
- ❌ Don't bypass the vault re-validation engine when checking if a document
  satisfies a tender requirement. No "we have insurance" shortcuts.
- ❌ Don't quote large blocks of a buyer's ITT verbatim into a draft response.
  Paraphrase.
- ❌ Don't hit real portal sites from CI. Use Playwright HAR replay.
- ❌ Don't log credentials, document contents, or PII.
- ❌ Don't edit a merged Alembic migration. Add a new one.
- ❌ Don't introduce `any` in TypeScript. Use `unknown` and narrow.

---

## 6. Recipes

### Adding a new tender source

1. Add a new module in `src/tender_agent/adapters/{code}.py`.
2. Subclass `SourceAdapter`. Implement `fetch_since` as an async generator yielding
   `NormalisedTender`.
3. If the source is OCDS, reuse `services/normaliser.ocds_release_to_tender`.
   Otherwise, add a converter function in `services/normaliser.py` and call it from
   your adapter.
4. Register the adapter in `adapters/__init__.py` under `ADAPTERS`.
5. Add the base URL to `config.Settings` and `.env.example`.
6. Add a fixture under `tests/fixtures/{code}_page.json` (or `.xml` for Atom-like
   sources) and a test file `tests/test_adapter_{code}.py` implementing the five
   patterns established in branch `t4-adapter-fixture-tests`:
   1. `test_fetch_since_yields_normalised_tenders` — full-fixture round-trip.
   2. `test_fetch_since_respects_cutoff` (post-filtering adapters only) **or**
      `test_fetch_since_sends_updated_from_param` (adapters that delegate the
      cutoff to the upstream query string).
   3. `test_normalisation_of_known_fields` — exact field mapping on one entry.
   4. `test_handles_missing_optional_fields` — None/empty stays None/empty.
   5. `test_handles_malformed_entry_gracefully` — bad entry skipped, others survive.

   All tests use `httpx.MockTransport` via the helpers in `tests/conftest.py`
   (`build_adapter`, `static_json_handler`/`static_text_handler`, `collect`,
   `load_json_fixture`/`load_text_fixture`). No network calls in tests, ever.
7. Update `PROJECT.md` §3 acceptance criteria if scope changes.

### Adding a new portal adapter (Phase 4)

1. Read `PROJECT.md` §5.7 for the full interface.
2. Create `src/tender_agent/services/portals/{portal}.py`.
3. Subclass `PortalAdapterBase` (provides browser context, retries, screenshot capture).
4. Put **all selectors as module-level constants** at the top of the file. Portals
   redesign; the file should be easy to repair.
5. Implement methods in order: `login`, `find_tender`, `download_documents`,
   `check_amendments`, `stage_submission`. **Do not implement `confirm_submission`
   until human-review UI is wired up.**
6. Record a HAR fixture for each method using staging credentials.
7. Tests run against the HAR; never against the real portal in CI.
8. Captcha = abort and signal human. Never solve.

### Adding a new vault document type (Phase 3)

1. Define the claims schema in `services/vault/claims_schemas.py` as a Pydantic
   model.
2. Add an extraction prompt in `services/vault/extractors/{type}.py`.
3. Register the type in the classifier mapping.
4. Add matching logic if it differs from the generic structured + semantic flow.
5. Tests: claims extraction round-trip, expiry handling, matcher behaviour on
   pass/partial/fail/expired/ambiguous cases.

### Generating a database migration

```bash
alembic revision -m "short description"     # creates new file
# edit the file, fill upgrade() and downgrade()
alembic upgrade head                        # apply locally
pytest                                      # verify nothing broke
```

---

## 7. Safety-critical guardrails

These exist because violating them creates real harm — legal, financial, or
reputational. They are not negotiable.

1. **Submission is human-gated.** The `submit` step on any portal adapter is only
   called via a Temporal workflow signal that originated from an authenticated user
   clicking Approve in the dashboard. There is no "auto-submit" flag, environment
   variable, config option, or feature flag that bypasses this. If you find yourself
   adding one, stop.

2. **Registration is human-gated.** Same as above, with the same prohibition.

3. **Outbound email to buyers is human-gated.** Drafts only. Sending is via the
   user's OAuth account after explicit approval.

4. **Vault re-validation is mandatory.** Code that asks "do we satisfy this
   requirement?" must go through the re-validation engine. No shortcuts via cached
   flags like `org.has_iso_27001`. Requirements depend on the *specific tender's*
   threshold, dates, and scope — and the answer can change between tenders even with
   the same documents.

5. **Vault documents are versioned, never deleted.** Supersede in place. Bids record
   the exact version submitted.

6. **Audit log everything that touches an external system or the vault.** Action,
   actor, timestamp, input hash, output hash, outcome.

7. **No credentials in code, DB plaintext, logs, or chat messages.**

8. **Copyright in drafting.** Don't reproduce ITT text verbatim in draft responses.
   Don't reproduce song lyrics, poems, or copyrighted content from web search anywhere.
   Paraphrase ITT requirements when echoing them back.

---

## 8. Working with the Claude API

The agent uses Anthropic Claude in several places:
- Requirements extractor (`services/requirements_extractor.py`)
- Claims extractor for vault ingestion (Phase 3)
- Drafting agent (Phase 5)
- Case study narrative generation (Phase 5)
- Inbound email classification (Phase 6)

Conventions:
- **Model selection**: Sonnet for drafting and reasoning, Haiku for classification
  and bulk extraction. Override per call as needed; default in `config.anthropic_model`.
- **System prompts** belong as module-level constants in the calling service.
- **Prompts include strict output schemas** and `confidence` fields where guessing
  is possible.
- **Never invent facts.** Every prompt to Claude includes "If the source doesn't
  say it, do not invent it. Mark ambiguity with confidence: 'low'."
- **Parse defensively**. JSON parsing is wrapped in try/except; failures are logged
  and the operation degrades gracefully.
- **Tool use** for the drafting agent. Tools are typed Pydantic models; the agent
  loop is bounded (max iterations, timeout).

---

## 9. When you're stuck

- Spec gap → say so in your response and ask. Don't guess past the gap.
- Live API behaves differently from coded assumption → flag, propose a fix to the
  adapter, and update the relevant note in PROJECT.md §5.1.
- A test you're sure should pass keeps failing → reduce to a minimal repro before
  changing more code.
- Portal selector keeps breaking → add a comment with the date and the portal
  version observed; consider whether the selector should be a config rather than a
  constant.

---

## 10. The bigger picture

This system is being built for a real business that wins real public-sector contracts.
Bids are legally binding. The compliance and human-review layers are the product, not
overhead. When in doubt, err on the side of more transparency, more human checkpoints,
more audit logging, and less autonomy.

The goal is an agent the company **trusts** to do the boring 90% so the human can
focus on the differentiating 10%. Trust is built by being predictable, conservative
about edge cases, and obviously correct in the audit trail.

That's the brief. Now go read PROJECT.md.
