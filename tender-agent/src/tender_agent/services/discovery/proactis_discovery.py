"""Proactis (procontract.due-north.com) opportunity discovery — chunk 9.

Pulls Proactis opportunities INTO the `tenders` table so the existing unified
search (chunk 7) finds them alongside CF/FTS, deduped by `procurement_ref`
(the "DN…" reference — same shape Proactis prints on the detail page and
Contracts Finder uses as procurement_ref).

Reuses:
- `services.portals.adapters.proactis` — PROACTIS_URLS / PROACTIS_SELECTORS,
  `is_authenticated`, the same single-bridge-session discipline.
- `services.bridge_client.BridgeClient` — navigate, fill, click, select-option,
  rendered-html, wait-for-login.
- `services.ingestion._upsert_tender` — UPSERT + change-hash + first_seen /
  last_seen.
- `services.deduplicator.find_duplicate` — cross-source dedup by
  procurement_ref (+ fuzzy buyer/title/value fallback).
- `services.regions.resolve_for_tender` — canonical region from buyer_region /
  raw addresses.

The flow:
1. ensure_authenticated (reuse adapter's is_authenticated). If not
   authenticated, wait for human login at the bridge window; if the wait
   times out, surface as `needs_login` and finish the run.
2. Navigate to /Opportunities/Index, APPLY the configured filters (keywords,
   regions, categories, include_closed=False), click Update.
3. WALK pages: read rows (Title, Buyer, Expression dates, Estimated value,
   the advertId from the Title link), follow Next until exhausted or
   `max_pages`.
4. For EACH opportunity row: open /Supplier/Advert/View?advertId=...,
   extract the DN reference + Region(s) of supply + Estimated value + dates
   + categories + description.
5. Build a `NormalisedTender` and call the SAME `_upsert_tender` CF/FTS use,
   so dedup and region resolution happen identically.
6. Record a `PollRun`-style audit row against the Proactis `Source`.

Source code: "PROACTIS" (a new entry alongside FTS/CF/PCS/S2W/NI). The
`tenders.source_ref` is the advertId (GUID, unique within Proactis);
`procurement_ref` is the DN reference — that's the cross-source dedup key.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import SessionLocal
from tender_agent.models import PollRun, Source, Tender
from tender_agent.schemas import NormalisedTender
from tender_agent.services.bridge_client import (
    BridgeClient,
    BridgeError,
    make_bridge_client,
)
from tender_agent.services.discovery.proactis_filter_config import (
    ProactisFilterConfig,
)
from tender_agent.services.discovery.proactis_login import (
    LoginAttempt,
    login_with_credentials,
)
from tender_agent.services.discovery.proactis_login_diagnostic import (
    capture_login_state,
)
from tender_agent.services.ingestion import _upsert_tender
from tender_agent.services.portals.adapters.proactis import (
    OPPORTUNITY_DATE_RE,
    OPPORTUNITY_ID_RE,
    OPPORTUNITY_VALUE_RE,
    PORTAL_OPTIONS_JS,
    PROACTIS_SELECTORS,
    PROACTIS_URLS,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Tender.source_code value for Proactis-discovered opportunities. Distinct
#: from the platform_slug "procontract" the document-fetch adapter uses —
#: source_code identifies the TENDER's origin; platform_slug identifies the
#: PORTAL adapter that can fetch its documents.
PROACTIS_SOURCE_CODE = "PROACTIS"

#: Slug passed to the bridge (same as the document-fetch adapter so the
#: human-supplied login is reused, not stolen).
PROACTIS_BRIDGE_SLUG = "procontract"

#: How long to wait at the bridge for a human to log in if `is_authenticated`
#: is False at the start of a run. Beyond this we surface `needs_login`.
LOGIN_WAIT_TIMEOUT_S = 600

#: How long to wait for the filtered listing to render after Update.
LISTING_RENDER_TIMEOUT_MS = 15_000


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredOpportunity:
    """Everything the listing + detail page give us about one opportunity.

    Constructed from rendered DOM (listing row) + plain page text (detail
    page). All fields except `advert_id` are best-effort: a Proactis page that
    omits the value or hides the buyer behind a logo doesn't break ingestion,
    it just yields a `NormalisedTender` with fewer fields populated.
    """

    advert_id: str  # the GUID from the listing link; source_ref
    dn_reference: str | None  # "DN815596" from the detail page; procurement_ref
    title: str
    buyer_name: str | None = None
    buyer_region: str | None = None  # raw text "Merseyside" / "North West"
    value_amount: Decimal | None = None
    value_currency: str = "GBP"
    expression_start: datetime | None = None
    expression_end: datetime | None = None
    contract_start: date | None = None
    contract_end: date | None = None
    description: str | None = None
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    detail_url: str | None = None

    def is_complete_for_dedup(self) -> bool:
        """A row is dedup-ready iff it has the DN reference, which is the
        cross-source key. Listing-only rows (no detail visit) aren't."""
        return bool(self.dn_reference)


@dataclass
class DiscoveryRunResult:
    """Summary of a single discovery run. Returned by `run()` and used by both
    the admin endpoint and the scheduler to build a structured log line."""

    status: str  # "ok" | "needs_login" | "error"
    pages_walked: int = 0
    rows_seen: int = 0
    opportunities_inserted: int = 0
    opportunities_updated: int = 0
    opportunities_unchanged: int = 0
    opportunities_deduped: int = 0  # row had a CF/FTS sibling by procurement_ref
    error: str | None = None
    poll_run_id: int | None = None
    # Filter-application telemetry (PR #102 — Dynatree popup driver). Populated
    # only on the profile-driven path; left at defaults for the legacy
    # public-listing `run()` so its output shape is unchanged.
    categories_requested: int = 0
    categories_applied: int = 0
    categories_not_found: list[str] = field(default_factory=list)
    regions_requested: int = 0
    regions_applied: int = 0
    regions_not_found: list[str] = field(default_factory=list)
    # Portal scoping (sister ProContract portals on the same instance — YPO,
    # The Chest, London Tenders, …). A name that doesn't match any node in
    # the Portals popup lands in portals_not_found, so a wrong entry is LOUD
    # in the run summary, never a silent zero-row run.
    portals_requested: int = 0
    portals_applied: int = 0
    portals_not_found: list[str] = field(default_factory=list)


class NeedsLoginError(BridgeError):
    """Raised internally when the bridge isn't authenticated and the wait
    timed out. The run captures this as `status=needs_login` rather than
    re-raising, so the scheduler doesn't trip-line the whole job queue."""


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def run(
    *,
    config: ProactisFilterConfig,
    bridge: BridgeClient | None = None,
    db_factory=SessionLocal,
) -> DiscoveryRunResult:
    """Run one Proactis discovery cycle and return a structured summary.

    `bridge` is injectable for tests; production passes None and the default
    `BridgeClient()` constructor (which reads `settings.bridge_url` +
    `settings.bridge_token`) is used.

    `db_factory` is injectable for tests; production uses `SessionLocal`.
    A run owns its own session for the lifetime of the cycle so partial
    progress is committed per-opportunity (mirrors `poll_source`).
    """
    bridge = bridge or make_bridge_client()
    result = DiscoveryRunResult(status="ok")

    # Open a session + PollRun row up front, so even an early failure
    # (auth, network) is auditable.
    with db_factory() as db:
        source = _ensure_proactis_source(db)
        poll_run = PollRun(source_id=source.id, status="running")
        db.add(poll_run)
        db.commit()
        db.refresh(poll_run)
        result.poll_run_id = poll_run.id

    logger.info(
        "discovery.proactis.start",
        poll_run_id=result.poll_run_id,
        constrained=config.is_constrained(),
        max_pages=config.max_pages,
    )

    try:
        # --- authenticate ---------------------------------------------------
        if not await _ensure_authenticated(bridge):
            result.status = "needs_login"
            _finalise_poll_run(db_factory, result)
            logger.info(
                "discovery.proactis.needs_login",
                poll_run_id=result.poll_run_id,
            )
            return result

        # --- apply filters --------------------------------------------------
        await _apply_filters(bridge, config)

        # --- walk listing ---------------------------------------------------
        listing_rows: list[DiscoveredOpportunity] = []
        async for row in _walk_listing(bridge, config):
            listing_rows.append(row)
            result.rows_seen += 1
        result.pages_walked = _last_walked_pages

        # --- read detail + upsert ------------------------------------------
        with db_factory() as db:
            for row in listing_rows:
                if config.skip_detail_for_known and _already_seen(db, row.advert_id):
                    # Touch last_seen_at via the same upsert path; we just
                    # don't hit the detail page.
                    continue
                detail = await _read_detail(bridge, row)
                if not detail.is_complete_for_dedup():
                    logger.warning(
                        "discovery.proactis.detail_missing_dn",
                        advert_id=detail.advert_id,
                    )
                action, deduped = _upsert_from_discovered(db, detail)
                if action == "new":
                    result.opportunities_inserted += 1
                elif action == "updated":
                    result.opportunities_updated += 1
                else:
                    result.opportunities_unchanged += 1
                if deduped:
                    result.opportunities_deduped += 1
                db.commit()

    except NeedsLoginError:
        result.status = "needs_login"
    except Exception as exc:  # noqa: BLE001 — surfaced via PollRun
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "discovery.proactis.failed", poll_run_id=result.poll_run_id
        )
    finally:
        # Always try to release the bridge session politely. Best-effort.
        try:
            await bridge.close_session(PROACTIS_BRIDGE_SLUG)
        except Exception:  # noqa: BLE001
            logger.debug("discovery.proactis.close_session_failed")
        _finalise_poll_run(db_factory, result)

    logger.info(
        "discovery.proactis.complete",
        poll_run_id=result.poll_run_id,
        status=result.status,
        pages=result.pages_walked,
        rows=result.rows_seen,
        inserted=result.opportunities_inserted,
        updated=result.opportunities_updated,
        deduped=result.opportunities_deduped,
    )
    return result


# ---------------------------------------------------------------------------
# Auth + filter application
# ---------------------------------------------------------------------------


async def _ensure_authenticated(bridge: BridgeClient) -> bool:
    """Open a bridge session if needed and check we're logged in.

    Returns False if the wait-for-login times out (operator hasn't completed
    the human login at the bridge window). Never submits credentials.
    """
    # Make sure a session exists for the slug. Best-effort: open is idempotent
    # on the bridge side.
    try:
        await bridge.open_session(
            PROACTIS_BRIDGE_SLUG, start_url=PROACTIS_URLS["post_login_home"]
        )
    except BridgeError as exc:
        logger.warning(
            "discovery.proactis.session_open_failed", error=str(exc)
        )
        return False

    # Probe authentication via the supplier home redirect rule.
    try:
        await bridge.navigate(
            PROACTIS_BRIDGE_SLUG, PROACTIS_URLS["post_login_home"]
        )
        status = await bridge.session_status(PROACTIS_BRIDGE_SLUG)
    except BridgeError:
        return False

    if not _is_login_url(status.get("current_url")):
        # Already at the supplier home → authenticated.
        return True

    # Bounced to /Login/Index — wait at the visible bridge window for the
    # operator to complete login. The pattern is "any URL that's not the
    # login page", served by Proactis post-auth redirects.
    logger.info("discovery.proactis.waiting_for_login")
    try:
        await bridge.wait_for_login(
            PROACTIS_BRIDGE_SLUG,
            success_url_pattern=r"^https://procontract\.due-north\.com/(?!Login).*",
            login_url=PROACTIS_URLS["login"],
            timeout_seconds=LOGIN_WAIT_TIMEOUT_S,
        )
    except BridgeError:
        return False

    # Re-probe — the login window may have closed but cookies set; navigating
    # back to the home should now succeed.
    try:
        await bridge.navigate(
            PROACTIS_BRIDGE_SLUG, PROACTIS_URLS["post_login_home"]
        )
        status = await bridge.session_status(PROACTIS_BRIDGE_SLUG)
    except BridgeError:
        return False
    return not _is_login_url(status.get("current_url"))


def _is_login_url(url: str | None) -> bool:
    """A URL is "still on the login page" iff it's the login path. Anything
    else means Proactis routed us into the authenticated area."""
    if not url:
        return False
    return "/Login" in url


async def _apply_filters(
    bridge: BridgeClient, config: ProactisFilterConfig
) -> None:
    """Navigate to Find Opportunities and apply each filter the config asks
    for. Skips controls the config doesn't constrain (empty list / blank
    string) so a stale Proactis browser state doesn't carry forward."""
    await bridge.navigate(PROACTIS_BRIDGE_SLUG, PROACTIS_URLS["opportunities"])

    # Keywords — single text box.
    if config.keywords.strip():
        await bridge.fill(
            PROACTIS_BRIDGE_SLUG,
            PROACTIS_SELECTORS["opp_keywords_input"],
            config.keywords.strip(),
        )

    # Include closed → "No" when we want open-only (the default). The control
    # is a select; "Yes"/"No" labels are visible to the operator.
    desired = "Yes" if config.include_closed else "No"
    try:
        await bridge.select_option(
            PROACTIS_BRIDGE_SLUG,
            PROACTIS_SELECTORS["opp_include_closed_select"],
            label=desired,
        )
    except BridgeError:
        # Some Proactis variants render this as a radio. Best-effort — the
        # default ("No") is already correct on most pages.
        logger.debug("discovery.proactis.include_closed_select_failed")

    # Regions — each adds a chip via the "Add new region" control. The button
    # opens an input/picker; we type the region name and confirm. Proactis's
    # control varies (autocomplete input vs select); fill+Enter handles the
    # common case.
    for region in config.regions:
        try:
            await bridge.click(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_add_region_button"],
            )
            await bridge.fill(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_region_input"],
                region,
            )
        except BridgeError as exc:
            logger.warning(
                "discovery.proactis.region_add_failed",
                region=region,
                error=str(exc),
            )

    for category in config.categories:
        try:
            await bridge.click(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_add_category_button"],
            )
            await bridge.fill(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_category_input"],
                category,
            )
        except BridgeError as exc:
            logger.warning(
                "discovery.proactis.category_add_failed",
                category=category,
                error=str(exc),
            )

    # Apply.
    await bridge.click(
        PROACTIS_BRIDGE_SLUG, PROACTIS_SELECTORS["opp_update_button"]
    )

    # Wait for the filtered listing to render before any pagination read.
    try:
        await bridge.rendered_html(
            PROACTIS_BRIDGE_SLUG,
            wait_for_selector=PROACTIS_SELECTORS["opp_results_table"],
            timeout_ms=LISTING_RENDER_TIMEOUT_MS,
        )
    except BridgeError:
        # Listing may legitimately be empty (no matches). Fall through; the
        # walk will yield zero rows and the run records 0 opportunities.
        logger.debug("discovery.proactis.listing_wait_timeout")

    logger.info(
        "discovery.proactis.filters_applied",
        keywords=bool(config.keywords.strip()),
        regions=len(config.regions),
        categories=len(config.categories),
        include_closed=config.include_closed,
    )


# ---------------------------------------------------------------------------
# Dynatree popup driver — categories + regions (PR #102)
# ---------------------------------------------------------------------------

#: How long to wait for the Dynatree popup to render after the trigger click.
DYNATREE_POPUP_WAIT_MS = 10_000
#: How long to wait for the tree to settle after each Search click. The XHR
#: is sub-second on the live site; this is a generous outer bound.
DYNATREE_SEARCH_WAIT_MS = 8_000


@dataclass
class PopupApplyOutcome:
    """The driver's report for one popup (categories OR regions). The
    discovery run rolls these counters up onto DiscoveryRunResult so the
    operator sees exactly which codes were and weren't matched on the first
    real run."""

    requested: int = 0
    applied: int = 0
    not_found: list[str] = field(default_factory=list)
    popup_opened: bool = False
    error: str | None = None


async def _popup_search(bridge: BridgeClient, term: str, popup_kind: str):
    """Drive ONE search inside an already-open Dynatree popup: clear the
    pre-filled placeholder, type `term`, re-assert Exact mode, click Search,
    wait for the tree (or the no-results marker) to settle. Returns the
    rendered page; raises BridgeError on a hard bridge failure so the caller
    can record the item and move on."""
    from tender_agent.services.discovery.proactis_dynatree import (
        DYNATREE_SELECTORS,
    )

    # Clear THEN fill — Proactis pre-fills the box with placeholder text as
    # the literal value, so a plain fill on top would corrupt the query.
    await bridge.fill(
        PROACTIS_BRIDGE_SLUG, DYNATREE_SELECTORS["search_input"], ""
    )
    await bridge.fill(
        PROACTIS_BRIDGE_SLUG, DYNATREE_SELECTORS["search_input"], term
    )

    # Belt-and-braces: ensure Exact match is selected. Default checked; a
    # no-op click on a checked radio is harmless.
    try:
        await bridge.click(
            PROACTIS_BRIDGE_SLUG, DYNATREE_SELECTORS["exact_match_radio"]
        )
    except BridgeError:
        # Some Proactis variants strip the radio when only one mode is
        # available. Don't fail the search over it.
        logger.debug(
            "discovery.proactis.exact_radio_missing", popup=popup_kind
        )

    await bridge.click(
        PROACTIS_BRIDGE_SLUG, DYNATREE_SELECTORS["search_button"]
    )

    # The XHR returns either a populated tree OR shows the no-results
    # marker; both selectors are in our wait expression.
    return await bridge.rendered_html(
        PROACTIS_BRIDGE_SLUG,
        wait_for_selector=DYNATREE_SELECTORS["tree_settled_wait"],
        timeout_ms=DYNATREE_SEARCH_WAIT_MS,
    )


async def _drive_dynatree_popup(
    bridge: BridgeClient,
    *,
    open_trigger_selector: str,
    items: list[str],
    matcher: str,  # "code" | "region"
    popup_kind: str,  # "category" | "region" — used for logs only
) -> PopupApplyOutcome:
    """Open the popup, search each item by EXACT match, tick the matching
    node, then click Apply once after every item has been processed. Items
    with no matching node are LOGGED and SKIPPED — never abort the run.

    Defensive about ordering: search input cleared before every type, Exact
    radio re-clicked defensively (default checked but a stale popup state
    might have flipped it).
    """
    # Imported here to keep the module import-cycle clean — Dynatree depends
    # on nothing from this module.
    from tender_agent.services.discovery.proactis_dynatree import (
        DYNATREE_SELECTORS,
        expand_cpv_prefix,
        match_node_for_code,
        match_node_for_region,
        no_results_visible,
        node_checkbox_selector,
        parse_dynatree_nodes,
    )

    outcome = PopupApplyOutcome(requested=len(items))

    # 1. Open the popup. The trigger is portal-specific (Add CPV / Add region);
    # the dialog itself is the SAME Dynatree component.
    try:
        await bridge.click(PROACTIS_BRIDGE_SLUG, open_trigger_selector)
    except BridgeError as exc:
        outcome.error = f"open trigger failed: {exc}"
        logger.warning(
            "discovery.proactis.popup_open_failed",
            popup=popup_kind,
            error=str(exc),
        )
        return outcome

    # Wait for the popup to render the search box.
    try:
        await bridge.rendered_html(
            PROACTIS_BRIDGE_SLUG,
            wait_for_selector=DYNATREE_SELECTORS["search_input"],
            timeout_ms=DYNATREE_POPUP_WAIT_MS,
        )
    except BridgeError as exc:
        outcome.error = f"popup wait failed: {exc}"
        return outcome

    outcome.popup_opened = True

    # 2. For each item: search (with the expanded CPV form first), parse the
    # settled tree, tick the match — or record not_found and continue.
    for raw in items:
        item = (raw or "").strip()
        if not item:
            continue

        # CPV codes: search AND match with the canonical 8-digit form. The
        # live CPV tree (filter-diagnostic, 2026-06-10) titles its nodes
        # "45000000-7 - Construction work" and its search matches code
        # prefixes, so "45000000" hits where the node-token comparison for a
        # bare "45" never could. The raw text stays as a second search in
        # case a tenant tree indexes the short form differently.
        if matcher == "code":
            target = expand_cpv_prefix(item)
            search_terms = [target] if target == item else [target, item]
        else:
            target = item
            search_terms = [item]

        node = None
        no_match_reason = "no search attempted"
        for attempt_index, term in enumerate(search_terms):
            try:
                rendered = await _popup_search(bridge, term, popup_kind)
            except BridgeError as exc:
                no_match_reason = f"search drive failed: {exc}"
                logger.warning(
                    "discovery.proactis.popup_search_failed",
                    popup=popup_kind,
                    item=item,
                    search_term=term,
                    error=str(exc),
                )
                break  # hard bridge failure — don't retry other terms

            if no_results_visible(rendered.html):
                no_match_reason = "divNoSearchResults visible"
                continue

            nodes = parse_dynatree_nodes(rendered.html)
            node = (
                match_node_for_code(nodes, target)
                if matcher == "code"
                else match_node_for_region(nodes, target)
            )
            if node is not None:
                if attempt_index > 0:
                    logger.info(
                        "discovery.proactis.popup_search_fallback",
                        popup=popup_kind,
                        item=item,
                        used=term,
                    )
                break
            no_match_reason = f"no node matched among {len(nodes)} results"

        if node is None:
            outcome.not_found.append(item)
            logger.info(
                "discovery.proactis.popup_no_match",
                popup=popup_kind,
                item=item,
                search_term=target,
                reason=no_match_reason,
            )
            continue

        # Tick the checkbox span. node_checkbox_selector emits a CSS
        # selector specific to THIS node so the click never lands on the
        # wrong row.
        tick_selector = node_checkbox_selector(node.node_id)
        try:
            await bridge.click(PROACTIS_BRIDGE_SLUG, tick_selector)
            outcome.applied += 1
            logger.info(
                "discovery.proactis.popup_ticked",
                popup=popup_kind,
                item=item,
                search_term=target,
                node_id=node.node_id,
                title=node.title_text,
            )
        except BridgeError as exc:
            outcome.not_found.append(item)
            logger.warning(
                "discovery.proactis.popup_tick_failed",
                popup=popup_kind,
                item=item,
                node_id=node.node_id,
                error=str(exc),
            )
            continue

    # 3. Apply once. If we ticked nothing, we still click Apply to close the
    # popup cleanly — leaving it open would block the next filter and the
    # subsequent listing read.
    try:
        await bridge.click(
            PROACTIS_BRIDGE_SLUG, DYNATREE_SELECTORS["apply_button"]
        )
    except BridgeError as exc:
        # If Apply genuinely failed, fall back to Cancel so the popup
        # doesn't stay open and block subsequent steps.
        outcome.error = (
            outcome.error or ""
        ) + f"apply failed: {exc}. Attempted cancel as fallback."
        with __import__("contextlib").suppress(Exception):
            await bridge.click(
                PROACTIS_BRIDGE_SLUG, DYNATREE_SELECTORS["cancel_link"]
            )
        logger.warning(
            "discovery.proactis.popup_apply_failed",
            popup=popup_kind,
            error=str(exc),
        )
        return outcome

    logger.info(
        "discovery.proactis.popup_applied",
        popup=popup_kind,
        requested=outcome.requested,
        applied=outcome.applied,
        not_found_count=len(outcome.not_found),
    )
    return outcome


async def _apply_filters_from_profile(
    bridge: BridgeClient,
    config: ProactisFilterConfig,
    result: DiscoveryRunResult,
) -> None:
    """Drive the Find Opportunities filters using the SAME approach the live
    Proactis dialogs require:

      * Keywords  → text-input (the legacy `opp_keywords_input` selector).
      * Include closed → "No" via the existing select.
      * Categories → Dynatree popup (search-by-CODE, exact match, tick the
        node, accumulate selections, click "Select categories").
      * Regions → Dynatree popup (search-by-NAME, tick the node, click
        "Select regions").

    Mutates `result` with the per-popup match counters so the run summary
    surfaces categories_applied / not_found and regions_applied / not_found
    for the operator's first real run.
    """
    await bridge.navigate(PROACTIS_BRIDGE_SLUG, PROACTIS_URLS["opportunities"])

    # Keywords — unchanged from the legacy text-input flow.
    if config.keywords.strip():
        try:
            await bridge.fill(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_keywords_input"],
                config.keywords.strip(),
            )
        except BridgeError as exc:
            logger.warning(
                "discovery.proactis.keywords_fill_failed", error=str(exc)
            )

    # Include closed — "No" when we want open-only (default).
    desired = "Yes" if config.include_closed else "No"
    try:
        await bridge.select_option(
            PROACTIS_BRIDGE_SLUG,
            PROACTIS_SELECTORS["opp_include_closed_select"],
            label=desired,
        )
    except BridgeError:
        # Some variants render it as radios; on most pages "No" is already
        # the default — skip silently.
        logger.debug("discovery.proactis.include_closed_select_failed")

    # Categories — Dynatree popup.
    if config.categories:
        cat_outcome = await _drive_dynatree_popup(
            bridge,
            open_trigger_selector=PROACTIS_SELECTORS["opp_add_category_button"],
            items=list(config.categories),
            matcher="code",
            popup_kind="category",
        )
        result.categories_requested = cat_outcome.requested
        result.categories_applied = cat_outcome.applied
        result.categories_not_found = list(cat_outcome.not_found)

    # Regions — same component, different trigger + matcher.
    if config.regions:
        reg_outcome = await _drive_dynatree_popup(
            bridge,
            open_trigger_selector=PROACTIS_SELECTORS["opp_add_region_button"],
            items=list(config.regions),
            matcher="region",
            popup_kind="region",
        )
        result.regions_requested = reg_outcome.requested
        result.regions_applied = reg_outcome.applied
        result.regions_not_found = list(reg_outcome.not_found)

    # Portals are NOT applied here. The live filter-diagnostic (2026-06-10)
    # proved there is no "Add portal" popup — portal scope is a plain
    # single-value <select> ("All" default). `run_for_profile` resolves the
    # configured names against the select's real options and loops the
    # listing walk per portal (see _resolve_portal_targets /
    # _apply_portal_selection). Leaving the select untouched here keeps the
    # proven all-portals default for the first Update below.

    # Apply the page-level filter form.
    try:
        await bridge.click(
            PROACTIS_BRIDGE_SLUG, PROACTIS_SELECTORS["opp_update_button"]
        )
    except BridgeError as exc:
        logger.warning(
            "discovery.proactis.update_click_failed", error=str(exc)
        )

    # Wait for the filtered listing to render before any pagination read.
    try:
        await bridge.rendered_html(
            PROACTIS_BRIDGE_SLUG,
            wait_for_selector=PROACTIS_SELECTORS["opp_results_table"],
            timeout_ms=LISTING_RENDER_TIMEOUT_MS,
        )
    except BridgeError:
        logger.debug("discovery.proactis.listing_wait_timeout")

    logger.info(
        "discovery.proactis.profile_filters_applied",
        keywords=bool(config.keywords.strip()),
        categories_requested=result.categories_requested,
        categories_applied=result.categories_applied,
        categories_not_found=len(result.categories_not_found),
        regions_requested=result.regions_requested,
        regions_applied=result.regions_applied,
        regions_not_found=len(result.regions_not_found),
        include_closed=config.include_closed,
    )


# ---------------------------------------------------------------------------
# Portal scope — a single-value <select>, looped per configured portal
# ---------------------------------------------------------------------------

def _match_portal_option(
    options: list[tuple[str, str]], name: str
) -> tuple[str, str] | None:
    """Resolve one configured portal NAME against the select's real options.

    Tolerance mirrors the region matcher: case-insensitive exact first, then
    prefix, then whole-substring — so "EastMidsTenders" matches exactly and
    "the chest" still finds "The Chest – North West". The "All" option is
    only reachable by EXACT match: a fuzzy hit on "All" would silently widen
    the scope, which is the opposite of what a configured name means."""
    target = (name or "").strip().lower()
    if not target:
        return None
    pairs = [((label or "").strip(), value) for label, value in options]
    for label, value in pairs:
        if label.lower() == target:
            return (label, value)
    for label, value in pairs:
        if label.lower() == "all":
            continue
        if label.lower().startswith(target):
            return (label, value)
    if len(target) >= 3:
        for label, value in pairs:
            if label.lower() == "all":
                continue
            if target in label.lower():
                return (label, value)
    return None


async def _resolve_portal_targets(
    bridge: BridgeClient,
    config: ProactisFilterConfig,
    result: DiscoveryRunResult,
) -> list[tuple[str, str | None]]:
    """Map the configured portal names onto the select's REAL options.

    Returns [(option_label, option_value)] for the names that resolved;
    unresolved names land in `result.portals_not_found` (logged, run
    continues — same loud-not-silent contract the popups have). When the
    bridge can't enumerate options (no `evaluate` — the HTTP bridge or test
    doubles), every name is returned with value=None and selection falls
    back to Playwright's exact-label match."""
    result.portals_requested = len(config.portals)

    options: list[tuple[str, str]] = []
    evaluate = getattr(bridge, "evaluate", None)
    if evaluate is not None:
        try:
            rows = await evaluate(PROACTIS_BRIDGE_SLUG, PORTAL_OPTIONS_JS)
        except BridgeError as exc:
            rows = None
            logger.warning(
                "discovery.proactis.portal_options_read_failed",
                error=str(exc),
            )
        if isinstance(rows, list):
            options = [
                (str(row.get("label") or ""), str(row.get("value") or ""))
                for row in rows
                if isinstance(row, dict)
            ]

    targets: list[tuple[str, str | None]] = []
    if not options:
        # Can't enumerate — defer to exact-label selection per name. A name
        # that doesn't exist will fail at selection time and be recorded
        # there, so the loud-miss contract still holds.
        logger.info(
            "discovery.proactis.portal_options_unavailable",
            fallback="exact-label select_option per configured name",
        )
        return [(name, None) for name in config.portals]

    for name in config.portals:
        matched = _match_portal_option(options, name)
        if matched is None:
            result.portals_not_found.append(name)
            logger.info(
                "discovery.proactis.portal_option_no_match",
                name=name,
                options_count=len(options),
            )
            continue
        label, value = matched
        if label != name:
            logger.info(
                "discovery.proactis.portal_option_fuzzy_match",
                configured=name,
                matched_label=label,
            )
        targets.append((label, value))
    return targets


async def _apply_portal_selection(
    bridge: BridgeClient, label: str, value: str | None
) -> bool:
    """Select ONE portal in the dropdown, click Update, wait for the listing
    to re-render. Returns True when the selection + update went through."""
    try:
        if value:
            await bridge.select_option(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_portal_select"],
                value=value,
            )
        else:
            await bridge.select_option(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_portal_select"],
                label=label,
            )
        await bridge.click(
            PROACTIS_BRIDGE_SLUG, PROACTIS_SELECTORS["opp_update_button"]
        )
    except BridgeError as exc:
        logger.warning(
            "discovery.proactis.portal_select_failed",
            portal=label,
            error=str(exc),
        )
        return False
    try:
        await bridge.rendered_html(
            PROACTIS_BRIDGE_SLUG,
            wait_for_selector=PROACTIS_SELECTORS["opp_results_table"],
            timeout_ms=LISTING_RENDER_TIMEOUT_MS,
        )
    except BridgeError:
        logger.debug("discovery.proactis.portal_listing_wait_timeout")
    logger.info("discovery.proactis.portal_scope_applied", portal=label)
    return True


# ---------------------------------------------------------------------------
# Listing walk
# ---------------------------------------------------------------------------

# Module-level so `run()` can read it after the async generator finishes
# without a class wrapper. Updated only by `_walk_listing`.
_last_walked_pages = 0


async def _walk_listing(bridge: BridgeClient, config: ProactisFilterConfig):
    """Yield one `DiscoveredOpportunity` (listing-only fields) per row, across
    every page up to `max_pages`. Pagination uses Proactis's "Next" link;
    when it's absent, we're on the last page."""
    global _last_walked_pages
    _last_walked_pages = 0
    seen_advert_ids: set[str] = set()

    for page_index in range(config.max_pages):
        _last_walked_pages = page_index + 1
        rendered = await bridge.rendered_html(
            PROACTIS_BRIDGE_SLUG,
            wait_for_selector=PROACTIS_SELECTORS["opp_results_table"],
            timeout_ms=LISTING_RENDER_TIMEOUT_MS,
        )
        rows = _parse_listing_rows(rendered.html)
        # Dedupe within the run — pagination glitches that show the same
        # page twice shouldn't double-count.
        new_rows = [r for r in rows if r.advert_id not in seen_advert_ids]
        for r in new_rows:
            seen_advert_ids.add(r.advert_id)
            yield r
        logger.info(
            "discovery.proactis.page",
            page=_last_walked_pages,
            rows=len(rows),
            new_rows=len(new_rows),
        )
        if not new_rows:
            # An empty / repeating page means we've fallen off the end.
            break

        # Try to advance. If there's no Next link, this page was the last.
        try:
            has_next = await bridge.element_exists(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_next_page_link"],
            )
        except BridgeError:
            has_next = False
        if not has_next:
            break
        try:
            await bridge.click(
                PROACTIS_BRIDGE_SLUG,
                PROACTIS_SELECTORS["opp_next_page_link"],
            )
        except BridgeError as exc:
            logger.warning(
                "discovery.proactis.next_page_failed", error=str(exc)
            )
            break


_LISTING_ROW_RE = re.compile(
    # Each row's title cell is a link of the form
    #   <a href="/Supplier/Advert/View?advertId=GUID">Title text</a>
    # Sometimes the href is just the path; sometimes absolute. We don't care —
    # we extract the advertId GUID from the query string.
    r'<a[^>]+href="[^"]*advertId=(?P<advert_id>[0-9a-fA-F-]{8,})"[^>]*>'
    r"(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


def _parse_listing_rows(html: str) -> list[DiscoveredOpportunity]:
    """Pull (advert_id, title) pairs out of the rendered listing HTML.

    Listing-only fields: just `advert_id` + `title`. Buyer / value /
    expression dates are stored on the same row but Proactis renders them
    with markup variations across portals — we read them off the DETAIL page
    where they're labelled, not from this table. Keeps the listing parser
    cheap and robust.
    """
    rows: list[DiscoveredOpportunity] = []
    for match in _LISTING_ROW_RE.finditer(html):
        advert_id = match.group("advert_id")
        title = _strip_tags(match.group("title")).strip()
        if not title:
            continue
        detail_url = PROACTIS_URLS["advert"] % advert_id
        rows.append(
            DiscoveredOpportunity(
                advert_id=advert_id,
                dn_reference=None,
                title=title,
                detail_url=detail_url,
            )
        )
    return rows


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    return _TAG_RE.sub("", html)


# ---------------------------------------------------------------------------
# Detail read
# ---------------------------------------------------------------------------


async def _read_detail(
    bridge: BridgeClient, row: DiscoveredOpportunity
) -> DiscoveredOpportunity:
    """Open the detail page for `row` and fill in the remaining fields.

    The detail page is text-heavy and label-driven. We read the plain page
    text (faster + more robust than HTML scraping) and pick fields out by
    well-known label proximity.
    """
    detail_url = row.detail_url or (PROACTIS_URLS["advert"] % row.advert_id)
    await bridge.navigate(PROACTIS_BRIDGE_SLUG, detail_url)
    text = await bridge.page_text(PROACTIS_BRIDGE_SLUG)

    # DN reference — the dedup key.
    m = OPPORTUNITY_ID_RE.search(text)
    row.dn_reference = m.group(0) if m else None

    # Estimated value.
    m_val = OPPORTUNITY_VALUE_RE.search(text)
    if m_val:
        with __import__("contextlib").suppress(InvalidOperation):
            row.value_amount = Decimal(m_val.group(1).replace(",", ""))

    # Region(s) of supply — labelled section. Capture the line that follows.
    row.buyer_region = _value_after_label(text, "Region(s) of supply") or \
        _value_after_label(text, "Regions of supply") or \
        _value_after_label(text, "Region")

    # Buyer — labelled "Buyer" or "Authority" depending on tenant config.
    row.buyer_name = _value_after_label(text, "Buyer") or \
        _value_after_label(text, "Authority")

    # Description — sits under a "Description" heading, often multi-line.
    row.description = _block_after_label(text, "Description", max_chars=2000)

    # Categories + keywords — comma-or-newline-separated under their labels.
    row.categories = _list_after_label(text, "Categories")
    row.keywords = _list_after_label(text, "Keywords")

    # Expression-of-interest window dates.
    row.expression_start = _date_after_label(text, "Expression start")
    row.expression_end = (
        _date_after_label(text, "Expression end")
        or _date_after_label(text, "Closing date")
        or _date_after_label(text, "Expression of interest end")
    )

    # Contract dates.
    cs = _date_after_label(text, "Contract start")
    ce = _date_after_label(text, "Contract end")
    row.contract_start = cs.date() if cs else None
    row.contract_end = ce.date() if ce else None

    return row


def _value_after_label(text: str, label: str) -> str | None:
    """Return the trimmed first non-empty line after `label:` in `text`.

    Proactis renders labels either inline (``"Buyer: Foo Council"``) or on
    the line above the value. Both shapes are handled.
    """
    lower_text = text.lower()
    lower_label = label.lower()
    idx = lower_text.find(lower_label)
    if idx == -1:
        return None
    tail = text[idx + len(label):]
    # Strip optional trailing colon + whitespace.
    tail = tail.lstrip(":").strip("  \t")
    # First non-empty line.
    for line in tail.splitlines():
        line = line.strip()
        if line:
            return line[:512]
    return None


def _block_after_label(text: str, label: str, max_chars: int = 2000) -> str | None:
    """Like `_value_after_label` but returns up to `max_chars` of trailing
    text — for multi-line descriptions."""
    lower_text = text.lower()
    lower_label = label.lower()
    idx = lower_text.find(lower_label)
    if idx == -1:
        return None
    tail = text[idx + len(label):].lstrip(":").strip()
    return tail[:max_chars] or None


def _list_after_label(text: str, label: str) -> list[str]:
    """Comma- or newline-separated values after `label`."""
    raw = _value_after_label(text, label)
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[,\n]", raw) if p.strip()]
    return parts[:50]  # cap absurd inputs


def _date_after_label(text: str, label: str) -> datetime | None:
    """Find the first dd/mm/yyyy date appearing within 80 chars after `label`."""
    lower_text = text.lower()
    lower_label = label.lower()
    idx = lower_text.find(lower_label)
    if idx == -1:
        return None
    window = text[idx + len(label): idx + len(label) + 200]
    m = OPPORTUNITY_DATE_RE.search(window)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def _upsert_from_discovered(
    db: Session, opp: DiscoveredOpportunity
) -> tuple[str, bool]:
    """Convert a `DiscoveredOpportunity` into a `NormalisedTender` and feed it
    through the existing CF/FTS upsert path.

    Returns (action, deduped) where:
      - action in {"new", "updated", "unchanged"} — from `_upsert_tender`.
      - deduped is True iff this new row was linked to an existing CF/FTS
        record via `duplicate_of_id` (cross-source dedup happened).
    """
    normalised = NormalisedTender(
        source_code=PROACTIS_SOURCE_CODE,
        source_ref=opp.advert_id,
        source_url=opp.detail_url or (PROACTIS_URLS["advert"] % opp.advert_id),
        procurement_ref=opp.dn_reference,
        title=opp.title or "(untitled)",
        description=opp.description,
        notice_type="opportunity",
        status="active",  # listing filters scoped to open-only by default
        buyer_name=opp.buyer_name,
        buyer_country="United Kingdom",
        buyer_region=opp.buyer_region,
        cpv_codes=[],
        keywords=opp.keywords,
        value_amount=opp.value_amount,
        value_currency=opp.value_currency,
        deadline_at=opp.expression_end,
        contract_start=opp.contract_start,
        contract_end=opp.contract_end,
        documents=[],
        raw={
            "advert_id": opp.advert_id,
            "dn_reference": opp.dn_reference,
            "discovered_via": "proactis",
            "categories": opp.categories,
            "expression_start": _iso(opp.expression_start),
            "expression_end": _iso(opp.expression_end),
        },
    )
    tender, action = _upsert_tender(db, normalised)
    deduped = action == "new" and tender.duplicate_of_id is not None
    return action, deduped


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _already_seen(db: Session, advert_id: str) -> bool:
    existing = db.execute(
        select(Tender.id).where(
            Tender.source_code == PROACTIS_SOURCE_CODE,
            Tender.source_ref == advert_id,
        )
    ).first()
    return existing is not None


# ---------------------------------------------------------------------------
# Source row + PollRun bookkeeping
# ---------------------------------------------------------------------------


def _ensure_proactis_source(db: Session) -> Source:
    """Get-or-create the PROACTIS `Source` row. Proactis isn't in the
    `ADAPTERS` registry (because it's not an HTTP-poll source), but a Source
    row gives `PollRun` something to FK to."""
    existing = db.execute(
        select(Source).where(Source.code == PROACTIS_SOURCE_CODE)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    source = Source(
        code=PROACTIS_SOURCE_CODE,
        name="Proactis (procontract.due-north.com)",
        base_url="https://procontract.due-north.com",
        enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _finalise_poll_run(
    db_factory, result: DiscoveryRunResult
) -> None:
    """Write the result counters back to the PollRun row."""
    if result.poll_run_id is None:
        return
    with db_factory() as db:
        run = db.get(PollRun, result.poll_run_id)
        if run is None:
            return
        run.finished_at = datetime.now(UTC)
        run.status = result.status
        run.fetched = result.rows_seen
        run.new_count = result.opportunities_inserted
        run.updated_count = result.opportunities_updated
        run.error = result.error
        db.commit()


# Re-export the helpers tests want to drive directly.
__all__ = [
    "DiscoveredOpportunity",
    "DiscoveryRunResult",
    "NeedsLoginError",
    "PROACTIS_BRIDGE_SLUG",
    "PROACTIS_SOURCE_CODE",
    "_apply_filters",
    "_parse_listing_rows",
    "_read_detail",
    "_upsert_from_discovered",
    "_walk_listing",
    "run",
]


# Convenience for the admin endpoint: a sync wrapper so a thread-pool
# handler can fire the coroutine without needing its own event loop dance.
def run_blocking(config: ProactisFilterConfig) -> DiscoveryRunResult:
    """Synchronous wrapper for ``run``. Used by the admin manual-trigger
    endpoint when it executes the discovery in a background thread."""
    return asyncio.run(run(config=config))


# ---------------------------------------------------------------------------
# Profile-driven, logged-in path (Step 1: single account, single profile)
# ---------------------------------------------------------------------------


async def _login_then_get_status(
    bridge: BridgeClient,
    credentials,  # services.portals.base.Credentials
) -> tuple[LoginAttempt, str | None]:
    """Open the bridge session, drive the login form with the supplied
    credentials, and return the LoginAttempt + the post-login current URL
    (for diagnostics). Never raises — the caller maps the LoginAttempt onto
    a DiscoveryRunResult shape."""
    try:
        await bridge.open_session(
            PROACTIS_BRIDGE_SLUG, start_url=PROACTIS_URLS["login"]
        )
    except BridgeError as exc:
        return (
            LoginAttempt(status="error", detail=f"open_session failed: {exc}"),
            None,
        )

    # Quick "are we ALREADY logged in?" probe — the bridge persistent context
    # may have surviving cookies from a previous run, in which case we skip
    # the form fill entirely.
    try:
        await bridge.navigate(
            PROACTIS_BRIDGE_SLUG, PROACTIS_URLS["post_login_home"]
        )
        status = await bridge.session_status(PROACTIS_BRIDGE_SLUG)
        if not _is_login_url(status.get("current_url")):
            return (
                LoginAttempt(
                    status="ok",
                    detail="session reused (cookies survived)",
                    current_url=status.get("current_url"),
                ),
                status.get("current_url"),
            )
    except BridgeError:
        pass

    attempt = await login_with_credentials(
        bridge, slug=PROACTIS_BRIDGE_SLUG, credentials=credentials
    )
    if attempt.status != "ok":
        # The session is still open and the page still shows whatever blocked
        # us (block page / cookie wall / unrecognised form / redirect target).
        # Snapshot it NOW — `run_for_profile` closes the session right after
        # this returns. Logs `discovery.proactis.login_diagnostic`; must never
        # mask the original outcome, hence best-effort.
        try:
            await capture_login_state(
                bridge, PROACTIS_BRIDGE_SLUG, attempt=attempt
            )
        except Exception:  # noqa: BLE001
            logger.debug("discovery.proactis.login_diagnostic_failed")
    return attempt, attempt.current_url


async def run_for_profile(
    *,
    credentials,  # services.portals.base.Credentials
    profile,  # tender_agent.models.FilterProfile
    bridge: BridgeClient | None = None,
    db_factory=SessionLocal,
) -> DiscoveryRunResult:
    """Logged-in, profile-filtered Proactis discovery (Step 1).

    Drives the operator's existing FilterProfile (CPV codes/prefixes +
    regions + any keywords) through Proactis as a SEARCH user. Reuses the
    existing parse + upsert + dedup path so nothing about the unified
    search semantics changes — same `source_code="PROACTIS"`,
    `source_ref=advertId`, `procurement_ref=DN`, region resolver.

    The login form is filled from `credentials` (the SAME `CredentialsStore`
    that powers Delta — secret never logged, fingerprint only). A rejected
    credential / missing form field / persistent /Login URL all map to a
    `needs_login` outcome with an explanatory `detail`; the run finishes
    cleanly so the operator can act.

    NOT in scope:
      * Single-Delta-session lease style locking — Proactis behaviour TBD.
      * Multi-user plumbing — single operator, single profile.

    PR #102 update: categories and regions now go through the Dynatree
    popup driver (`_apply_filters_from_profile` → `_drive_dynatree_popup`),
    which searches by code/name, ticks the matching node's checkbox span,
    and clicks the popup's apply button. Codes/regions that don't match
    anything in the tree are logged + recorded in the run summary
    (categories_not_found / regions_not_found) and the run continues.
    """
    bridge = bridge or make_bridge_client()
    # Portal scope is deployment-level config (PROACTIS_DISCOVERY_PORTALS),
    # not a per-profile field — the FilterProfile has no portals dimension.
    # Empty (the default) leaves the Portals control untouched, exactly the
    # proven first-run behaviour.
    from tender_agent.config import settings as _settings

    config = ProactisFilterConfig.from_filter_profile(
        profile, portals=list(_settings.proactis_discovery_portals)
    )
    result = DiscoveryRunResult(status="ok")

    with db_factory() as db:
        source = _ensure_proactis_source(db)
        poll_run = PollRun(source_id=source.id, status="running")
        db.add(poll_run)
        db.commit()
        db.refresh(poll_run)
        result.poll_run_id = poll_run.id

    logger.info(
        "discovery.proactis.profile_start",
        poll_run_id=result.poll_run_id,
        profile_id=getattr(profile, "id", None),
        constrained=config.is_constrained(),
        cpv_count=len(config.categories),
        region_count=len(config.regions),
    )

    try:
        attempt, current_url = await _login_then_get_status(bridge, credentials)
        if attempt.status != "ok":
            result.status = "needs_login"
            result.error = (
                f"login {attempt.status}: {attempt.detail or 'no detail'}"
            )
            _finalise_poll_run(db_factory, result)
            logger.info(
                "discovery.proactis.login_blocked",
                poll_run_id=result.poll_run_id,
                outcome=attempt.status,
                current_url=current_url,
            )
            return result

        # PR #102: profile-driven runs use the Dynatree popup driver for
        # categories/regions (search-by-code, exact match, tick the
        # checkbox span, click apply). Keywords + include_closed still go
        # through the text-input flow inside _apply_filters_from_profile.
        await _apply_filters_from_profile(bridge, config, result)

        # Portal scope: the select is SINGLE-value ("…WithAllOptionFilter",
        # default "All"), so multiple configured portals mean one
        # select → Update → walk cycle per portal, de-duped by advertId
        # across cycles. No portals configured = the select stays on "All"
        # and we walk once — the proven default.
        listing_rows: list[DiscoveredOpportunity] = []
        seen_advert_ids: set[str] = set()
        portal_targets: list[tuple[str, str | None]] = []
        if config.portals:
            portal_targets = await _resolve_portal_targets(
                bridge, config, result
            )
        if portal_targets:
            total_pages = 0
            for portal_label, portal_value in portal_targets:
                selected = await _apply_portal_selection(
                    bridge, portal_label, portal_value
                )
                if not selected:
                    result.portals_not_found.append(portal_label)
                    continue
                result.portals_applied += 1
                async for row in _walk_listing(bridge, config):
                    if row.advert_id in seen_advert_ids:
                        continue
                    seen_advert_ids.add(row.advert_id)
                    listing_rows.append(row)
                    result.rows_seen += 1
                total_pages += _last_walked_pages
            result.pages_walked = total_pages
        else:
            # Either no portals configured (select stays on "All") or none
            # of the configured names resolved (loud in the summary below;
            # the run continues un-scoped rather than yielding nothing).
            async for row in _walk_listing(bridge, config):
                listing_rows.append(row)
                result.rows_seen += 1
            result.pages_walked = _last_walked_pages
        if config.portals:
            logger.info(
                "discovery.proactis.portal_scope_summary",
                requested=result.portals_requested,
                applied=result.portals_applied,
                not_found=list(result.portals_not_found),
            )

        with db_factory() as db:
            for row in listing_rows:
                if (
                    config.skip_detail_for_known
                    and _already_seen(db, row.advert_id)
                ):
                    continue
                detail = await _read_detail(bridge, row)
                if not detail.is_complete_for_dedup():
                    logger.warning(
                        "discovery.proactis.detail_missing_dn",
                        advert_id=detail.advert_id,
                    )
                action, deduped = _upsert_from_discovered(db, detail)
                if action == "new":
                    result.opportunities_inserted += 1
                elif action == "updated":
                    result.opportunities_updated += 1
                else:
                    result.opportunities_unchanged += 1
                if deduped:
                    result.opportunities_deduped += 1
                db.commit()
    except NeedsLoginError:
        result.status = "needs_login"
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "discovery.proactis.profile_failed", poll_run_id=result.poll_run_id
        )
    finally:
        try:
            await bridge.close_session(PROACTIS_BRIDGE_SLUG)
        except Exception:  # noqa: BLE001
            logger.debug("discovery.proactis.close_session_failed")
        _finalise_poll_run(db_factory, result)

    logger.info(
        "discovery.proactis.profile_complete",
        poll_run_id=result.poll_run_id,
        status=result.status,
        pages=result.pages_walked,
        rows=result.rows_seen,
        inserted=result.opportunities_inserted,
        updated=result.opportunities_updated,
        deduped=result.opportunities_deduped,
    )
    return result


def run_for_profile_blocking(
    *,
    credentials,
    profile,
) -> DiscoveryRunResult:
    """Synchronous wrapper for ``run_for_profile``. Used by the admin
    manual-trigger endpoint when it runs discovery in a background thread."""
    return asyncio.run(run_for_profile(credentials=credentials, profile=profile))
