"""FastAPI entrypoint."""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from tender_agent import scheduler
from tender_agent.api import accounts as accounts_api
from tender_agent.api import admin as admin_api
from tender_agent.api import billing as billing_api
from tender_agent.api import credentials as credentials_api
from tender_agent.api import filters as filters_api
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
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


app = FastAPI(
    title="Tender Agent",
    version="0.1.0",
    description="UK public tender discovery and bid automation agent",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


app.include_router(tenders_api.router)
app.include_router(filters_api.router)
app.include_router(push_api.router)
app.include_router(admin_api.router)
app.include_router(vault_api.router)
app.include_router(portals_api.router)
app.include_router(platforms_api.router)
app.include_router(credentials_api.router)
app.include_router(tender_fetch_api.router)
app.include_router(tender_brief_api.router)
app.include_router(system_api.router)
app.include_router(accounts_api.router)
app.include_router(accounts_api.me_router)
app.include_router(billing_api.router)
app.include_router(submission_api.router)
