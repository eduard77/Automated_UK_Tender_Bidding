"""FastAPI entrypoint."""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tender_agent import scheduler
from tender_agent.api import accounts as accounts_api
from tender_agent.api import admin as admin_api
from tender_agent.api import admin_diagnostics as admin_diagnostics_api
from tender_agent.api import admin_portals as admin_portals_api
from tender_agent.api import billing as billing_api
from tender_agent.api import credentials as credentials_api
from tender_agent.api import document_ingest as document_ingest_api
from tender_agent.api import email as email_api
from tender_agent.api import filters as filters_api
from tender_agent.api import go_no_go as go_no_go_api
from tender_agent.api import platforms as platforms_api
from tender_agent.api import portals as portals_api
from tender_agent.api import push as push_api
from tender_agent.api import submission as submission_api
from tender_agent.api import system as system_api
from tender_agent.api import tender_brief as tender_brief_api
from tender_agent.api import tender_fetch as tender_fetch_api
from tender_agent.api import tenders as tenders_api
from tender_agent.api import vault as vault_api
from tender_agent.config import settings


def _configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stdout)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def _run_startup_migrations() -> None:
    """Apply pending Alembic migrations against the configured DB.

    The cloud deploy path is browser-only (merge to main -> GitHub Action ->
    new container) — there is no terminal step where the operator could run
    `alembic upgrade head`, so the app brings its own schema up to date on
    boot. Idempotent: when the DB is already at head this is a no-op, and the
    docker-compose dev flow (which migrates before uvicorn) is unaffected.
    Disable with MIGRATE_ON_STARTUP=false. Failures are logged loudly but
    never stop the app from booting (mirrors the preflight philosophy)."""
    from pathlib import Path

    from alembic.config import Config

    from alembic import command

    logger = structlog.get_logger(__name__)
    # main.py lives at <root>/src/tender_agent/main.py; alembic.ini sits at
    # <root>/ both locally and in the runtime image (/app).
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    try:
        command.upgrade(cfg, "head")
        logger.info("startup.migrations_applied")
    except Exception as exc:  # noqa: BLE001 - never fatal at boot
        logger.error("startup.migrations_failed", error=str(exc))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
    if settings.migrate_on_startup:
        _run_startup_migrations()
    # Self-diagnosing startup check: log whether the documents dir is writable,
    # the bridge token is set, and the bridge is reachable. Best-effort and
    # never fatal — it just makes a first-run misconfiguration explain itself.
    import contextlib

    from tender_agent.services.preflight import run_preflight

    with contextlib.suppress(Exception):
        await run_preflight(check_bridge=True)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()
        # Close any in-process Playwright contexts cleanly. Idempotent and a
        # no-op when the in-process driver was never used (HTTP-bridge mode
        # or no portal traffic this run).
        with contextlib.suppress(Exception):
            from tender_agent.services.bridge_in_process import (
                shutdown_in_process_bridge,
            )

            await shutdown_in_process_bridge()


app = FastAPI(
    title="Tender Agent",
    version="0.1.0",
    description="UK public tender discovery and bid automation agent",
    lifespan=lifespan,
)

# CORS — explicit allow-list of dashboard origins from settings.
# The dashboard calls every endpoint with `credentials: "include"` to carry
# the session cookie, which means the browser will refuse a wildcard
# `Access-Control-Allow-Origin: *`. Stick to the configured list. See
# Settings.cors_allow_origins for the env-var override.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


app.include_router(tenders_api.router)
app.include_router(filters_api.router)
app.include_router(push_api.router)
app.include_router(admin_api.router)
app.include_router(admin_diagnostics_api.router)
app.include_router(admin_portals_api.router)
app.include_router(vault_api.router)
app.include_router(portals_api.router)
app.include_router(platforms_api.router)
app.include_router(credentials_api.router)
app.include_router(tender_fetch_api.router)
app.include_router(document_ingest_api.router)
app.include_router(tender_brief_api.router)
app.include_router(system_api.router)
app.include_router(accounts_api.router)
app.include_router(accounts_api.me_router)
app.include_router(billing_api.router)
app.include_router(go_no_go_api.router)
app.include_router(submission_api.router)
app.include_router(email_api.router)
