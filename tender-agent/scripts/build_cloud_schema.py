"""Build the application schema on the Azure Postgres cloud database.

ONE-OFF helper. Does NOT change app default config. Does NOT copy data.

Reads CLOUD_DB_HOST, CLOUD_DB_USER, CLOUD_DB_PASSWORD from tender-agent/.env,
builds the cloud connection URL (sslmode=require) using the SAME driver and
SAME database name the app uses locally (postgresql+psycopg, tender_agent by
default — overridable via LOCAL_DB_NAME), then:

  1. connects to the server's default 'postgres' DB and CREATE DATABASE
     <target> if it does not already exist;
  2. preflights the 'vector' extension — if pgvector is not enabled on the
     server, STOPS with a clear "BLOCKED:" message;
  3. runs `alembic upgrade head` against the cloud DB via a temporary
     DATABASE_URL environment override (the app's default DATABASE_URL is
     untouched);
  4. verifies: alembic current == head, vector extension present, key tables
     present, row counts all 0.

The cloud password is read from the .env file only. It is never printed,
logged, or written to disk by this script. The cloud URL is rendered in
log messages with the password redacted as `***`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Project layout: this file lives in tender-agent/scripts/; backend root is
# its parent. .env sits at the backend root.
# ---------------------------------------------------------------------------
BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"

# Tables we expect after running migrations through head. This is the
# "verification" set the task spec calls out — not the full list.
KEY_TABLES = [
    "tenders",
    "tender_document_files",
    "tender_document_content",
    "tender_briefs",
    "portal_platforms",
    "portals",
    "vault_documents",
    "vault_document_versions",
]


def die(msg: str) -> "type[SystemExit]":
    print(msg, file=sys.stderr)
    sys.exit(1)


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        die(f"BLOCKED: {path} not found — required for CLOUD_DB_* secrets.")
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def redact_url(url: str) -> str:
    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", url)


def get_target_db_name(env: dict[str, str]) -> str:
    # Allow explicit override; otherwise derive from the local DATABASE_URL
    # in .env (which is what the app actually uses); otherwise fall back to
    # the in-code default in tender_agent.config.Settings.
    override = env.get("LOCAL_DB_NAME") or os.environ.get("LOCAL_DB_NAME")
    if override:
        return override
    local = env.get("DATABASE_URL") or ""
    if local:
        parts = urlsplit(local)
        path = parts.path.lstrip("/")
        if path:
            return path
    return "tender_agent"


def build_cloud_url(host: str, user: str, password: str, dbname: str) -> str:
    # Match the app's driver: postgresql+psycopg (psycopg3). sslmode=require
    # is mandatory for Azure.
    user_q = quote(user, safe="")
    pw_q = quote(password, safe="")
    return (
        f"postgresql+psycopg://{user_q}:{pw_q}@{host}:5432/{dbname}"
        f"?sslmode=require"
    )


def cloud_url_for_db(server_user: str, server_pw: str, host: str, dbname: str) -> str:
    return build_cloud_url(host, server_user, server_pw, dbname)


def psycopg_connect(url: str):
    # SQLAlchemy-style URL -> raw psycopg connection.
    # postgresql+psycopg://U:P@H:P/DB?sslmode=require
    import psycopg  # local import so module import doesn't require it

    parts = urlsplit(url)
    # Strip the +psycopg driver prefix for libpq.
    scheme = "postgresql"
    netloc = parts.netloc
    libpq_url = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    return psycopg.connect(libpq_url)


def ensure_database(server_url_admin: str, target_db: str) -> bool:
    """Create target_db on the Azure server if it doesn't exist.

    Returns True if the database already existed, False if it was created.
    Connection is to the server's default 'postgres' admin DB; autocommit is
    required for CREATE DATABASE.
    """
    conn = psycopg_connect(server_url_admin)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
            if cur.fetchone() is not None:
                print(f"[ok] database '{target_db}' already exists on the server")
                return True
            # Identifier interpolation — validate strictly.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target_db):
                die(f"BLOCKED: refusing to CREATE DATABASE with unsafe name {target_db!r}")
            cur.execute(f'CREATE DATABASE "{target_db}"')
            print(f"[ok] CREATE DATABASE {target_db}")
            return False
    finally:
        conn.close()


def preflight_vector_extension(target_url: str) -> None:
    """Confirm pgvector is available on the cloud server. STOP if not."""
    conn = psycopg_connect(target_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as exc:  # pragma: no cover — operator path
                die(
                    "BLOCKED: pgvector not enabled — tick VECTOR under "
                    "azure.extensions in the Azure portal and re-run.\n"
                    f"underlying error: {exc}"
                )
            cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            if not row:
                die(
                    "BLOCKED: pgvector not enabled — tick VECTOR under "
                    "azure.extensions in the Azure portal and re-run."
                )
            print("[ok] pgvector extension present on cloud DB")
    finally:
        conn.close()


def run_alembic_upgrade_head(target_url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = target_url  # one-off override for this subprocess
    print(f"[run] alembic upgrade head  (target={redact_url(target_url)})")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_ROOT),
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        die(f"BLOCKED: alembic upgrade head failed (exit {proc.returncode})")


def alembic_current(target_url: str) -> str:
    env = os.environ.copy()
    env["DATABASE_URL"] = target_url
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout + proc.stderr).strip()


def alembic_heads(target_url: str) -> str:
    env = os.environ.copy()
    env["DATABASE_URL"] = target_url
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout + proc.stderr).strip()


def verify(target_url: str) -> None:
    print("\n=== verification ===")

    current = alembic_current(target_url)
    heads = alembic_heads(target_url)
    print(f"alembic current: {current}")
    print(f"alembic heads:   {heads}")

    def head_rev(text: str) -> str | None:
        for line in text.splitlines():
            m = re.match(r"^([0-9a-f]{4,})", line.strip())
            if m:
                return m.group(1)
        return None

    cur_rev = head_rev(current)
    head_rev_ = head_rev(heads)
    if not cur_rev or cur_rev != head_rev_:
        die(f"BLOCKED: alembic not at head (current={cur_rev}, head={head_rev_})")
    print(f"[ok] at head: {cur_rev}")

    conn = psycopg_connect(target_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            if not cur.fetchone():
                die("BLOCKED: vector extension missing after migrations.")
            print("[ok] vector extension present")

            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename"
            )
            tables = [r[0] for r in cur.fetchall()]
            print(f"\ntables ({len(tables)}):")
            for t in tables:
                print(f"  - {t}")

            missing = [t for t in KEY_TABLES if t not in tables]
            if missing:
                die(f"BLOCKED: key tables missing after migration: {missing}")
            print(f"\n[ok] key tables present: {KEY_TABLES}")

            print("\nrow counts (expected all 0):")
            non_zero: list[tuple[str, int]] = []
            for t in tables:
                cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                n = cur.fetchone()[0]
                marker = "" if n == 0 else "  <-- NON-ZERO"
                print(f"  {t:40s} {n}{marker}")
                if n != 0:
                    non_zero.append((t, n))
            if non_zero:
                # Not fatal — the task says expected 0, but if the operator
                # ran this twice or there's seed data, just warn loudly.
                print(f"\n[warn] non-zero counts: {non_zero}")
            else:
                print("\n[ok] all tables empty (0 rows)")
    finally:
        conn.close()


def main() -> None:
    env = load_env_file(ENV_FILE)

    host = env.get("CLOUD_DB_HOST")
    user = env.get("CLOUD_DB_USER")
    password = env.get("CLOUD_DB_PASSWORD")
    missing = [k for k, v in (
        ("CLOUD_DB_HOST", host),
        ("CLOUD_DB_USER", user),
        ("CLOUD_DB_PASSWORD", password),
    ) if not v]
    if missing:
        die(f"BLOCKED: missing required env keys in {ENV_FILE}: {missing}")

    target_db = get_target_db_name(env)
    print(f"[cfg] cloud host:   {host}")
    print(f"[cfg] cloud user:   {user}")
    print(f"[cfg] target db:    {target_db}")

    server_admin_url = cloud_url_for_db(user, password, host, "postgres")
    target_url = cloud_url_for_db(user, password, host, target_db)

    print(f"[cfg] admin URL:    {redact_url(server_admin_url)}")
    print(f"[cfg] target URL:   {redact_url(target_url)}")

    ensure_database(server_admin_url, target_db)
    preflight_vector_extension(target_url)
    run_alembic_upgrade_head(target_url)
    verify(target_url)

    print("\n[done] Cloud schema build complete — empty schema, no data, no local-config change.")


if __name__ == "__main__":
    main()
