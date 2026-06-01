TASK: Build the cloud database schema on Azure Postgres (migrations only — NO data, NO local-config change)

GOAL (plain English)
We have a new managed PostgreSQL 16 database in Azure (server: genera-system-db). It is empty. Build the full application schema on it by running the EXISTING Alembic migrations against it, so the app can run against the cloud database from any machine. This must NOT copy any data (separate later step), must NOT change how the app connects locally by default, and must NOT hardcode, print, log, or commit any secret.

INPUTS (already placed by the user in the backend .env — read them, never print them)
- CLOUD_DB_HOST = genera-system-db.postgres.database.azure.com
- CLOUD_DB_USER = Eduard_Szigeti
- CLOUD_DB_PASSWORD = (secret, in .env only)
- Azure REQUIRES SSL: use sslmode=require on every connection.

STEPS
1. git pull origin main first (avoid the phantom-merge trap; ensure the latest migrations are present).
2. Discover the backend's DB config: find how it builds its SQLAlchemy/Alembic connection — the exact driver (psycopg2 vs psycopg vs asyncpg) and the local database NAME. Reuse the SAME driver/URL format and the SAME database name for the cloud.
3. Read CLOUD_DB_* from the backend .env. Build the cloud connection URL in the app's expected format, with sslmode=require. Never echo the password anywhere.
4. On the Azure server, connect to the default "postgres" database and CREATE DATABASE <local-db-name> if it does not already exist.
5. Run the existing migrations (alembic upgrade head) against that cloud database. This is a ONE-OFF run pointed at the cloud URL via an env override / alembic -x / temporary config — do NOT change the app's default DATABASE_URL or local config.
6. Verify on the cloud DB and report:
   - alembic current == head
   - the "vector" extension exists (SELECT extname FROM pg_extension WHERE extname='vector')
   - list created tables; confirm the key ones exist (tenders, tender_document_files, tender_document_content, tender_briefs, portal_platforms, portals, and the vault tables)
   - row counts (all should be 0 — empty schema)
7. If the "vector" extension is missing, STOP with: "BLOCKED: pgvector not enabled — tick VECTOR under azure.extensions in the Azure portal and re-run."

RULES
- NO data copied or migrated. Empty schema only.
- NO change to default/local connection. The cloud run is an explicit one-off override.
- NEVER hardcode, print, log, or commit CLOUD_DB_PASSWORD or any secret — read from .env only.
- Do not weaken SSL (sslmode=require).
- If any env key is missing, the host is unreachable, or pgvector is off, put "BLOCKED:" at the top stating exactly what's needed. Do not guess.
- Build from current main.

SHIP
This is mainly a live ops action. Commit ONLY this prompt doc and any small reusable helper you create (e.g. scripts/build_cloud_schema.py) — NEVER the .env. Open a PR titled "feat(cloud): build Azure Postgres schema (migrations only, no data)". In the description, paste the verification output (tables created, at head, pgvector present, 0 rows). Sentinel at top: "Cloud schema build — empty schema only, no data, no local-config change."

Begin.
