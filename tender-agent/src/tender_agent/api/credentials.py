"""Credentials API — admin-only management of portal logins.

Passwords are write-only: they go in via POST and are never returned. The
store encrypts them at rest with a key in the OS keyring.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.api.schemas.credentials import (
    CredentialCreate,
    CredentialMetadataRead,
    CredentialValidateResponse,
)
from tender_agent.db import get_db
from tender_agent.models import Portal
from tender_agent.services.credentials import CredentialsStoreError, get_store
from tender_agent.services.portals.base import Credentials

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/credentials", tags=["credentials"])

DEFAULT_USER = "eduard"


def _portal_lookup(db: Session, portal_ids: list[int]) -> dict[int, Portal]:
    if not portal_ids:
        return {}
    rows = db.execute(
        select(Portal).where(Portal.id.in_(portal_ids))
    ).scalars().all()
    return {p.id: p for p in rows}


@router.get("", response_model=list[CredentialMetadataRead])
def list_credentials(db: Session = Depends(get_db)) -> list[CredentialMetadataRead]:
    store = get_store()
    try:
        metas = store.list_credentials(DEFAULT_USER)
    except CredentialsStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    portals = _portal_lookup(db, [m.portal_id for m in metas])
    out: list[CredentialMetadataRead] = []
    for m in metas:
        portal = portals.get(m.portal_id)
        out.append(
            CredentialMetadataRead(
                portal_id=m.portal_id,
                platform_slug=m.platform_slug,
                valid=m.valid,
                last_used_at=m.last_used_at,
                last_validated_at=m.last_validated_at,
                portal_domain=portal.domain if portal else None,
                portal_display_name=portal.display_name if portal else None,
            )
        )
    return out


@router.post("", response_model=CredentialMetadataRead, status_code=201)
def upsert_credentials(
    body: CredentialCreate, db: Session = Depends(get_db)
) -> CredentialMetadataRead:
    portal = db.get(Portal, body.portal_id)
    if portal is None:
        raise HTTPException(status_code=404, detail="portal not found")
    platform_slug = portal.platform.slug if portal.platform else None
    store = get_store()
    creds = Credentials(
        username=body.username,
        password=body.password,
        email=body.email,
        extra=body.extra,
    )
    try:
        store.store_credentials(
            body.portal_id, DEFAULT_USER, creds, platform_slug=platform_slug
        )
    except CredentialsStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return CredentialMetadataRead(
        portal_id=body.portal_id,
        platform_slug=platform_slug,
        valid=True,
        last_used_at=None,
        last_validated_at=None,
        portal_domain=portal.domain,
        portal_display_name=portal.display_name,
    )


@router.delete("/{portal_id}", status_code=204)
def delete_credentials(portal_id: int) -> None:
    store = get_store()
    try:
        store.delete_credentials(portal_id, DEFAULT_USER)
    except CredentialsStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{portal_id}/validate", response_model=CredentialValidateResponse)
async def validate_credentials(
    portal_id: int, db: Session = Depends(get_db)
) -> CredentialValidateResponse:
    portal = db.get(Portal, portal_id)
    if portal is None:
        raise HTTPException(status_code=404, detail="portal not found")
    store = get_store()
    try:
        creds = store.get_credentials(portal_id, DEFAULT_USER)
    except CredentialsStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if creds is None:
        raise HTTPException(status_code=404, detail="no credentials stored")

    # Call the platform adapter's authenticate() with the stored creds.
    from tender_agent.services.portals.base import PortalContext
    from tender_agent.services.portals.registry import (
        get_adapter_for_platform,
        get_fallback_adapter,
    )
    from tender_agent.services.portals.results import AuthStatus

    platform_slug = portal.platform.slug if portal.platform else None
    adapter_cls = get_adapter_for_platform(platform_slug) or get_fallback_adapter()
    adapter = adapter_cls()
    ctx = PortalContext(
        portal_id=portal_id, user_id=DEFAULT_USER, domain=portal.domain
    )
    try:
        result = await adapter.authenticate(ctx, creds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("credentials.validate_failed", error=str(exc))
        store.mark_invalid(portal_id, DEFAULT_USER)
        return CredentialValidateResponse(
            portal_id=portal_id, valid=False, status="error", detail=str(exc)
        )

    if result.status == AuthStatus.success:
        store.mark_validated(portal_id, DEFAULT_USER)
        return CredentialValidateResponse(
            portal_id=portal_id, valid=True, status=str(result.status)
        )
    store.mark_invalid(portal_id, DEFAULT_USER)
    return CredentialValidateResponse(
        portal_id=portal_id,
        valid=False,
        status=str(result.status),
        detail=result.detail,
    )
