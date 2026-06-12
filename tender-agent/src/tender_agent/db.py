from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tender_agent.config import settings

# Postgres session guards (2026-06-12 discovery outage). The poll cycle runs
# sync SQLAlchemy sessions inside asyncio tasks; a session blocked on a row
# lock (e.g. two concurrent polls upserting the same tender, one holding its
# transaction open across an HTTP await) waits FOREVER by default — wedging
# the event loop and, with APScheduler max_instances=1, every future cycle.
# lock_timeout turns that wedge into an error after 30 s; the
# idle-in-transaction timeout kills transactions abandoned mid-flight (a hung
# task's open transaction would otherwise hold its row locks indefinitely).
# Single-flight polling prevents the contention by design — these are the
# belt-and-braces so NO variant of it can hang the app again.
_connect_args: dict = {}
if settings.database_url.startswith("postgresql"):
    _connect_args = {
        "options": "-c lock_timeout=30000 -c idle_in_transaction_session_timeout=600000"
    }

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
