"""Match a portal domain to a PortalPlatform via the platform's domain_patterns.

The patterns are stored on the platform row (seeded by migration 0006) so the
DB is the single source of truth. New portals discovered after a platform
exists are linked at ingestion time via `find_platform_for_domain`.
"""
from __future__ import annotations

import re

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import PortalPlatform

logger = structlog.get_logger(__name__)


def domain_matches(domain: str, patterns: list[str]) -> bool:
    """True if `domain` matches any of the regex `patterns`."""
    for pattern in patterns:
        try:
            if re.search(pattern, domain):
                return True
        except re.error:
            logger.warning("platform_matching.bad_pattern", pattern=pattern)
    return False


def find_platform_for_domain(db: Session, domain: str) -> PortalPlatform | None:
    """Return the first platform whose domain_patterns match `domain`, or None.

    Iteration order is by id (seed order), so the first-seeded platform wins
    when patterns overlap — keep patterns disjoint to avoid surprises.
    """
    platforms = db.execute(
        select(PortalPlatform).order_by(PortalPlatform.id)
    ).scalars().all()
    for platform in platforms:
        if domain_matches(domain, platform.domain_patterns or []):
            return platform
    return None
