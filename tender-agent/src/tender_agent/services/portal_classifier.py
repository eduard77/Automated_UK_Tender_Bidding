"""Claude-powered classification of a discovered portal.

`schedule_classification(portal_id)` is the public entry point used by the
ingestion pipeline. It enqueues a job onto a singleton worker thread that
processes portals with a minimum 3-second gap (avoid hammering Claude's API
during a large backfill).

`classify_portal(portal_id, db)` performs one classification synchronously
and is the test surface (mock this in unit tests).

If `ANTHROPIC_API_KEY` is unset or the call fails, the portal record is
updated with `classification_data = {"status": "claude_unavailable", ...}`
and we move on. No exception propagates to ingestion.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import SessionLocal
from tender_agent.models import Portal, PortalUrlSighting

logger = structlog.get_logger(__name__)


CLASSIFICATION_SYSTEM_PROMPT = """You are a procurement domain analyst. Given a \
portal homepage and a sample tender URL, decide whether this is a procurement \
portal (where buyers publish ITT documents and suppliers register interest) \
and characterise it.

Strict rules:
- Base every field on the supplied evidence. If the evidence is thin, say so \
in `reasoning` and lower `confidence`.
- `confidence` is your subjective probability the classification is correct. \
0.0 means "no idea", 1.0 means "certain".
- `suggested_priority` reflects strategic value to a UK construction / public \
sector bidder: critical = national framework / many active tenders, high = \
established regional portal, medium = single buyer with regular tenders, \
low = one-off / micro-buyer / placeholder.
"""

CLASSIFICATION_USER_TEMPLATE = """Domain: {domain}
Sample tender URL: {sample_url}

Truncated homepage HTML (first ~5000 chars):
---
{html}
---

Return ONLY a JSON object with this exact schema, no preamble, no markdown \
fencing:

{{
  "is_procurement_portal": boolean,
  "confidence": number,
  "suggested_display_name": string,
  "login_type": "none" | "email_only" | "username_password" | "2fa" | "oauth" | "unknown",
  "suggested_priority": "critical" | "high" | "medium" | "low",
  "reasoning": string,
  "notes": string
}}
"""


HIGH_CONFIDENCE_THRESHOLD = 0.7
HTML_TRUNCATE_CHARS = 5_000
MIN_GAP_SECONDS = 3.0


@dataclass(frozen=True)
class ClassificationResult:
    portal_id: int
    status: str  # 'classified' | 'fetch_failed' | 'claude_unavailable' | 'parse_failed' | 'skipped'
    confidence: float | None = None
    raw: dict[str, Any] | None = None


# --- HTTP fetch helper --------------------------------------------------


def _fetch_homepage(domain: str, *, timeout: float = 10.0) -> str | None:
    """Best-effort GET of https://{domain}/. Returns the response text or
    None if anything goes wrong. Logs but never raises."""
    url = f"https://{domain}/"
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.http_user_agent},
        ) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                logger.info(
                    "portal_classifier.fetch_non_ok",
                    domain=domain,
                    status=resp.status_code,
                )
                return None
            return resp.text
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "portal_classifier.fetch_failed",
            domain=domain,
            error=str(exc),
        )
        return None


def _truncate_html(html: str) -> str:
    if len(html) <= HTML_TRUNCATE_CHARS:
        return html
    # Keep head + first slice of body — most signals live there.
    head_end = html.lower().find("</head>")
    if 0 < head_end < HTML_TRUNCATE_CHARS // 2:
        head = html[: head_end + len("</head>")]
        remaining = HTML_TRUNCATE_CHARS - len(head)
        return head + html[head_end + len("</head>") : head_end + len("</head>") + remaining]
    return html[:HTML_TRUNCATE_CHARS]


# --- Claude call (mockable) ---------------------------------------------


def _call_claude(domain: str, sample_url: str | None, html: str) -> dict[str, Any] | None:
    """Returns the parsed JSON dict from Claude, or None on any failure."""
    if not settings.anthropic_api_key:
        return None
    try:
        from anthropic import Anthropic  # noqa: PLC0415

        client = Anthropic(api_key=settings.anthropic_api_key)
        user_prompt = CLASSIFICATION_USER_TEMPLATE.format(
            domain=domain,
            sample_url=sample_url or "(none)",
            html=_truncate_html(html),
        )
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=CLASSIFICATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "portal_classifier.claude_call_failed",
            domain=domain,
            error=str(exc),
        )
        return None

    text_blocks = [
        getattr(b, "text", "") for b in getattr(response, "content", [])
        if getattr(b, "type", None) == "text"
    ]
    raw = "\n".join(text_blocks).strip()
    if raw.startswith("```"):
        # Drop opening fence + optional language tag, and the closing fence.
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "portal_classifier.parse_failed",
            domain=domain,
            error=str(exc),
        )
        return None


# Tests monkey-patch this hook. Production code calls _call_claude through it.
classifier_backend: Callable[[str, str | None, str], dict[str, Any] | None] = _call_claude


# --- Persistence --------------------------------------------------------


def _sample_sighting_url(db: Session, portal_id: int) -> str | None:
    return db.execute(
        select(PortalUrlSighting.url)
        .where(PortalUrlSighting.portal_id == portal_id)
        .where(PortalUrlSighting.sighting_type.in_(("tender_link", "document_link")))
        .order_by(PortalUrlSighting.extracted_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _apply_classification(portal: Portal, parsed: dict[str, Any]) -> None:
    """Write the Claude JSON onto the Portal record. High-confidence fields
    overwrite defaults; otherwise we keep the placeholder values so a human
    can override manually."""
    portal.classification_data = {
        **parsed,
        "_status": "classified",
        "_classified_at": datetime.now(UTC).isoformat(),
    }

    confidence = parsed.get("confidence")
    high = isinstance(confidence, (int, float)) and confidence >= HIGH_CONFIDENCE_THRESHOLD
    if high:
        suggested_name = parsed.get("suggested_display_name")
        if isinstance(suggested_name, str) and suggested_name.strip():
            portal.display_name = suggested_name.strip()
        login = parsed.get("login_type")
        if isinstance(login, str) and login in (
            "none",
            "email_only",
            "username_password",
            "2fa",
            "oauth",
            "unknown",
        ):
            portal.login_type = login
        priority = parsed.get("suggested_priority")
        if isinstance(priority, str) and priority in ("critical", "high", "medium", "low"):
            portal.priority = priority
    portal.updated_at = datetime.now(UTC)


def classify_portal(portal_id: int, db: Session) -> ClassificationResult:
    """Single, synchronous classification. Safe to call directly from tests."""
    portal = db.get(Portal, portal_id)
    if portal is None:
        return ClassificationResult(portal_id=portal_id, status="skipped")

    sample_url = _sample_sighting_url(db, portal_id)

    html = _fetch_homepage(portal.domain)
    if html is None:
        portal.classification_data = {
            "_status": "fetch_failed",
            "_classified_at": datetime.now(UTC).isoformat(),
        }
        portal.updated_at = datetime.now(UTC)
        db.commit()
        return ClassificationResult(portal_id=portal_id, status="fetch_failed")

    parsed = classifier_backend(portal.domain, sample_url, html)
    if parsed is None:
        portal.classification_data = {
            "_status": "claude_unavailable",
            "_classified_at": datetime.now(UTC).isoformat(),
        }
        portal.updated_at = datetime.now(UTC)
        db.commit()
        return ClassificationResult(portal_id=portal_id, status="claude_unavailable")

    _apply_classification(portal, parsed)
    db.commit()
    confidence = parsed.get("confidence")
    return ClassificationResult(
        portal_id=portal_id,
        status="classified",
        confidence=confidence if isinstance(confidence, (int, float)) else None,
        raw=parsed,
    )


# --- Async queue --------------------------------------------------------


class _ClassificationQueue:
    """Singleton background worker. Cheap: one daemon thread, one queue,
    one Claude call every MIN_GAP_SECONDS. Drains naturally when the process
    exits — we don't enforce graceful shutdown because the worst case is a
    portal stays unclassified and gets picked up the next time it's
    re-sighted (or via POST /portals/{id}/classify)."""

    def __init__(self) -> None:
        self._queue: queue.Queue[int] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_run = 0.0

    def schedule(self, portal_id: int) -> None:
        self._queue.put(portal_id)
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="portal-classifier", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                portal_id = self._queue.get(timeout=30.0)
            except queue.Empty:
                return  # worker idle for a while — exit; new schedule() will respawn
            gap = time.monotonic() - self._last_run
            if gap < MIN_GAP_SECONDS:
                time.sleep(MIN_GAP_SECONDS - gap)
            self._last_run = time.monotonic()
            db = SessionLocal()
            try:
                classify_portal(portal_id, db)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "portal_classifier.worker_failed", portal_id=portal_id
                )
            finally:
                db.close()


_QUEUE = _ClassificationQueue()


def schedule_classification(portal_id: int) -> None:
    """Public scheduler — fire-and-forget. Tests can monkeypatch this to a
    no-op so they never enqueue real work."""
    _QUEUE.schedule(portal_id)
