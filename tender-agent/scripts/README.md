# tender-agent scripts

Operator scripts that touch the running stack. Each script can be run from
the host (with a local `DATABASE_URL`) or inside the app container.

## `backfill_filter_matches.py`

Re-scores every active filter profile against every non-duplicate tender and
inserts missing `FilterMatch` rows. Use this after creating a new filter
profile so the dashboard's `match_count` reflects the existing data, not
just tenders ingested after the profile was created.

```bash
# Inside the app container
docker compose exec -T --user root app /opt/venv/bin/python \
    /app/scripts/backfill_filter_matches.py --dry-run
docker compose exec -T --user root app /opt/venv/bin/python \
    /app/scripts/backfill_filter_matches.py
```

- `--dry-run` reports what would be inserted without writing.
- `--filter-id N` limits the run to one profile.
- Idempotent: re-runs report `already-matched=N, would-insert=0`.

## `seed_vault_placeholders.py`

Seeds 8 `VaultDocument` + `VaultDocumentVersion` rows with realistic but
fictional claims (insurance × 3, ISO accreditations × 4, accounts × 1) so the
dashboard's `/vault` page renders with structure on a fresh database. Every
title is prefixed `PLACEHOLDER -- ` so the dashboard's amber banner flags them.

```bash
# Inside the app container (where the default storage dir is writable)
docker compose exec -T --user root app /opt/venv/bin/python \
    /app/scripts/seed_vault_placeholders.py

# Re-seed after a previous run (deletes the old placeholders first)
docker compose exec -T --user root app /opt/venv/bin/python \
    /app/scripts/seed_vault_placeholders.py --force
```

Safety: refuses to run if `TENDER_AGENT_ENV=production`. Never point at a
production database — the placeholders are fictional and will pollute real
vault state.

Each version is backed by a small `.txt` blob under
`settings.document_storage_dir` (default `/var/tender-agent/documents`). The
storage layout mirrors what S3 will use in prod, so `storage_key` reads the
same under either backend.

## `backfill_portal_discovery.py`

Walks every existing tender through the portal-discovery pipeline. Creates
`Portal` rows for previously-unseen external domains, inserts
`PortalUrlSighting` rows for every URL / email surfaced from descriptions,
documents, and `additionalInformation`, and queues Claude classification for
each new portal. Safe to re-run: identical sightings are skipped.

```bash
docker compose exec -T --user root app /opt/venv/bin/python \
    /app/scripts/backfill_portal_discovery.py

# Skip classification (no Claude calls) — useful when ANTHROPIC_API_KEY
# is unset or you just want raw discovery against a fresh DB.
docker compose exec -T --user root app /opt/venv/bin/python \
    /app/scripts/backfill_portal_discovery.py --no-classify
```

Safety: refuses to run when `TENDER_AGENT_ENV=production`. Batched with
LIMIT/OFFSET (200 tenders per batch by default) — no server-side cursors,
deliberately, after the cursor-state bug from the previous prompt run.

## `validate_extractor.py`

Runs the requirements extractor against real tenders and produces a markdown
report covering schema conformance + grounding heuristics. See the script
docstring for flags.

```bash
docker compose exec -T --user root app /opt/venv/bin/python \
    /app/scripts/validate_extractor.py --recent 10 --output /tmp/report.md
```
