# 13 — Seed sample tenders into the CLOUD database for funnel testing

Companion to `10-cloud-db-schema.md`. Run AFTER the cloud schema has been
built; produces a small, clearly-marked, idempotent, reversible set of sample
tenders + one brief so the dashboard's paywall funnel can be exercised
end-to-end against the Azure-hosted database.

---

TASK: Seed a few sample tenders into the CLOUD database for funnel testing (idempotent, clearly-marked sample data)

GOAL (plain English)
The cloud database (genera-system-db) has the full schema but zero tenders, so the dashboard shows "0 tenders" and we can't test the paywall funnel. Insert a small set of realistic SAMPLE tenders into the CLOUD database so the dashboard has something to search and open. Generate ONE real brief for one of them so the brief teaser/paywall can be tested. This is test seed data, clearly marked, and safe to delete later. It must NOT touch the local database, must NOT alter the app's default config, and must NOT copy anything from the desktop.

CONNECTION (same approach as scripts/build_cloud_schema.py)
- Read CLOUD_DB_HOST / CLOUD_DB_USER / CLOUD_DB_PASSWORD from tender-agent/.env. Build the cloud URL with sslmode=require and dbname tender_agent, exactly as build_cloud_schema.py does. Reuse that helper's connection logic; do NOT reinvent it. Never print/log/commit the password.
- All writes go to the CLOUD database only. Do not touch local Postgres.

WHAT TO SEED (idempotent — safe to run twice)
1. ~6 sample tenders into the tenders table, realistic for UK construction/public-sector (titles, buyers, regions, sectors, contract values spread across bands, deadlines a mix of near and far, CPV/sector codes). Mark each clearly as sample: set a recognisable marker (e.g. source='SAMPLE_SEED' or a title prefix '[SAMPLE]') so they can be found and deleted. Use fixed deterministic IDs/refs so re-running UPSERTs rather than duplicates.
2. For ONE of the seeded tenders, write a complete tender_briefs row so the brief teaser can be tested: a realistic structured brief (recommendation bid/no_bid/conditional, key_risks, scope_summary, contract_value, deadline, mandatory_requirements, scoring) — either by calling the existing brief engine against the sample tender's text, OR if that needs documents we don't have, by inserting a hand-crafted but realistic brief JSON that matches the tender_briefs schema exactly. Prefer the real brief engine if it can run from tender text alone; otherwise insert a schema-valid sample brief and note in output which path was used.
3. Seed enough that the dashboard's "sources active" / counts render sensibly if those derive from tenders.

CONSTRAINTS
- Idempotent: re-running updates the same sample rows, never piles up duplicates.
- Clearly marked + reversible: provide a companion note (or a --clear flag on the script) that deletes exactly the sample rows by their marker, so cleanup is one command.
- Tenant/org: if tenders are org-scoped, attach them to a sample org consistent with how the app expects (check the model; reuse any existing default/sample org pattern rather than inventing one).
- No schema changes, no migrations, no LLM calls unless using the real brief engine for step 2 (and if so, mockable/keyed from env, and report the rough cost).
- Build from current main; git pull first. Do NOT change app default config or .env.

VERIFY + REPORT
- After seeding, query the cloud DB and report: count of sample tenders inserted, their titles + contract values + deadlines, and confirmation the one brief row exists and is schema-valid. State which brief path was used (real engine vs sample JSON).
- Remind me of the exact command to clear the sample data later.

SHIP
- This is a seed/ops action against the cloud. Commit ONLY the seed script (e.g. scripts/seed_cloud_samples.py) + this prompt doc. NEVER commit .env. Opening a PR is optional for a seed script — if you open one, title it "chore(seed): sample tenders in cloud for funnel testing" and paste the verification output. Sentinel at top: "Sample seed data — cloud only, idempotent, reversible, no app-config change."
- If blocked (e.g. can't reach cloud, missing env), put "BLOCKED:" at top with exactly what's needed.

Begin.
