"""Live Playwright driver for the dashboard bot.

Drives the deployed dashboard (https://genera-tenders-dashboard-…)
through a real Chromium: navigate /search, set the filter draft, press
Search, read the rendered result list, optionally click into a tender's
detail page and assert its CPV cell. Reuses the parser module so the
LIVE path and the UNIT path read the SAME markup with the SAME logic —
they cannot drift.

Cloud / disguise notes:
* From the Azure datacenter range the dashboard 403s on raw fetches
  (probed today). This bot inherits the in-process bridge's disguise
  (real Chromium UA, viewport, language) by reusing
  `bridge_in_process.cloud_browser_context_kwargs` /
  `apply_cloud_browser_stealth` — same shape that broke Proactis open.
* The operator typically runs this from a residential IP; CI does NOT
  hit the live URL by default (the e2e tests are marked and skipped).
* Pacing: between each filter interaction we sleep for a small,
  jittered human-ish delay so we don't look like a script. Configurable;
  default is short for fast feedback during a real run.

Headless by default. Set `DASHBOARD_BOT_HEADLESS=0` (or pass
`headless=False`) for an operator-watchable session.
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from tests.e2e.dashboard_bot.parser import (
    SearchResult,
    extract_cpv_cell,
    extract_results_from_dom,
    extract_source_chip_options,
)
from tests.e2e.dashboard_bot.report import BotReport, make_check_result
from tests.e2e.dashboard_bot.specs import (
    CheckOutcome,
    CheckSpec,
    FilterDraft,
    OpenTender,
    SearchExpectation,
)

logger = structlog.get_logger(__name__)

DEFAULT_DASHBOARD_URL = (
    "https://genera-tenders-dashboard-bgg7aqewf8f0c0ge.ukwest-01.azurewebsites.net"
)
SEARCH_PATH = "/search"

#: Pacing budgets. Conservative defaults; a spec can override per phase.
PACE_MIN_MS = 250
PACE_MAX_MS = 700
NAVIGATE_TIMEOUT_MS = 20_000
RESULTS_RENDER_TIMEOUT_MS = 15_000


async def _pace() -> None:
    """Sleep a random short interval — keeps the bot looking human-ish.
    Not a security feature; just polite to the live backend."""
    await asyncio.sleep(random.uniform(PACE_MIN_MS, PACE_MAX_MS) / 1000.0)


class DashboardBot:
    """One bot instance drives one browser context for a series of
    specs. Context is reused across specs so cookies / session survive,
    which matches the human path the bot is meant to verify."""

    def __init__(
        self,
        dashboard_url: str = DEFAULT_DASHBOARD_URL,
        *,
        headless: bool = True,
        screenshot_dir: str | None = None,
    ) -> None:
        self.dashboard_url = dashboard_url.rstrip("/")
        self.headless = headless
        self.screenshot_dir = screenshot_dir
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def __aenter__(self) -> DashboardBot:
        from playwright.async_api import async_playwright  # noqa: PLC0415

        # Reuse the proven cloud-disguise kwargs from the in-process
        # bridge so this bot inherits the same UA / viewport / locale —
        # the discipline note says "apply that pattern, don't
        # re-investigate from scratch".
        try:
            from tender_agent.services.bridge_in_process import (  # noqa: PLC0415
                apply_cloud_browser_stealth,
                cloud_browser_context_kwargs,
            )

            disguise_kwargs = cloud_browser_context_kwargs()
            apply_stealth = apply_cloud_browser_stealth
        except Exception:  # noqa: BLE001 - fall back to plain Chromium
            disguise_kwargs = {}

            async def apply_stealth(_: Any) -> None:
                return None

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(**disguise_kwargs)
        await apply_stealth(self._context)
        self._page = await self._context.new_page()
        return self

    async def __aexit__(self, *_: Any) -> None:
        for closer in (self._context, self._browser, self._playwright):
            if closer is None:
                continue
            with _suppress_exception():
                close = getattr(closer, "close", None) or getattr(
                    closer, "stop", None
                )
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result

    # --- the API the runner uses ------------------------------------------

    async def run_specs(self, specs: list[CheckSpec]) -> BotReport:
        report = BotReport(
            dashboard_url=self.dashboard_url,
            started_at=datetime.now(UTC),
        )
        for spec in specs:
            result = await self._run_one(spec)
            report.results.append(result)
        report.finished_at = datetime.now(UTC)
        return report

    async def _run_one(self, spec: CheckSpec):
        try:
            if spec.filter is None and spec.open is None:
                # Spec is "facet sanity" only — read the chips off /search.
                observed, details = await self._observe_facet(spec)
            else:
                await self._goto_search()
                if spec.filter is not None:
                    await self._apply_filter(spec.filter)
                    await self._press_search()
                observed, details = await self._evaluate(spec)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            logger.exception("dashboard_bot.spec_failed", spec=spec.name)
            return make_check_result(
                spec,
                observed=CheckOutcome.FAIL,
                error=f"{type(exc).__name__}: {exc}",
            )
        return make_check_result(
            spec,
            observed=observed,
            details=details,
            screenshot_path=await self._maybe_screenshot(spec.name)
            if observed != spec.expected_outcome
            else None,
        )

    # --- internals --------------------------------------------------------

    async def _goto_search(self) -> None:
        await self._page.goto(
            self.dashboard_url + SEARCH_PATH,
            wait_until="domcontentloaded",
            timeout=NAVIGATE_TIMEOUT_MS,
        )
        # The Source chips are rendered AFTER the facet fetch; wait on a
        # stable filter form marker rather than racing.
        with _suppress_exception():
            await self._page.wait_for_selector(
                "form[aria-label='Tender search filters']",
                timeout=NAVIGATE_TIMEOUT_MS,
            )
        await _pace()

    async def _observe_facet(
        self, _spec: CheckSpec
    ) -> tuple[CheckOutcome, list[str]]:
        await self._goto_search()
        html = await self._page.content()
        chips = extract_source_chip_options(html)
        return CheckOutcome.PASS, [f"source_chips: {', '.join(chips) or '<none>'}"]

    async def _apply_filter(self, draft: FilterDraft) -> None:
        page = self._page
        if draft.q is not None:
            await page.fill("input[placeholder*='school refurbishment' i]", draft.q)
            await _pace()
        if draft.cpv is not None:
            await page.fill("input[placeholder='45000000, 45210000']", draft.cpv)
            await _pace()
        if draft.buyer is not None:
            await page.fill("input[placeholder*='Bristol' i]", draft.buyer)
            await _pace()
        if draft.value_min is not None:
            await page.fill("input[placeholder='50000']", draft.value_min)
            await _pace()
        if draft.value_max is not None:
            await page.fill("input[placeholder='500000']", draft.value_max)
            await _pace()
        for source in draft.sources:
            # Source chips render with the FRIENDLY label (sourceLabel())
            # but specs name the raw code; the SOURCE_LABELS map lives on
            # the dashboard side, so we drive by friendly label.
            await page.get_by_role(
                "button", name=_friendly_source_label(source)
            ).click()
            await _pace()

    async def _press_search(self) -> None:
        await self._page.get_by_role("button", name="Search").click()
        with _suppress_exception():
            await self._page.wait_for_selector(
                "section ul, h2:has-text('No tenders match these filters.')",
                timeout=RESULTS_RENDER_TIMEOUT_MS,
            )
        await _pace()

    async def _evaluate(
        self, spec: CheckSpec
    ) -> tuple[CheckOutcome, list[str]]:
        details: list[str] = []
        html = await self._page.content()
        results = extract_results_from_dom(html)
        details.append(f"results_count={len(results)}")
        details.append(
            "sources_in_results: "
            + ", ".join(sorted({r.source_code for r in results}) or ["<none>"])
        )
        observed = CheckOutcome.PASS

        exp = spec.search
        if exp is not None:
            observed = _verdict_for_search(exp, results, details)

        # If the search-side already lined up with the spec's prediction,
        # and an `open` is requested, evaluate that too — its verdict
        # supersedes only on FAIL.
        if observed != CheckOutcome.FAIL and spec.open is not None:
            open_observed = await self._evaluate_open(spec.open, results, details)
            if open_observed == CheckOutcome.FAIL:
                observed = CheckOutcome.FAIL
            elif open_observed == CheckOutcome.OBSERVED_GAP:
                observed = CheckOutcome.OBSERVED_GAP
        return observed, details

    async def _evaluate_open(
        self,
        open_spec: OpenTender,
        results: list[SearchResult],
        details: list[str],
    ) -> CheckOutcome:
        if not results:
            details.append("open: no results to open")
            return CheckOutcome.FAIL
        target = results[
            min(open_spec.result_index, len(results) - 1)
        ]
        await self._page.goto(
            f"{self.dashboard_url}/tenders/{target.tender_id}",
            wait_until="domcontentloaded",
            timeout=NAVIGATE_TIMEOUT_MS,
        )
        await _pace()
        detail_html = await self._page.content()
        cpv_value = extract_cpv_cell(detail_html)
        details.append(
            f"open_tender_id={target.tender_id} cpv_cell={cpv_value!r}"
        )
        verdict = CheckOutcome.PASS
        if open_spec.cpv_field_present is True and not cpv_value:
            verdict = CheckOutcome.OBSERVED_GAP
        if open_spec.title_non_empty and not target.title:
            verdict = CheckOutcome.FAIL
        return verdict

    async def _maybe_screenshot(self, label: str) -> str | None:
        if self.screenshot_dir is None or self._page is None:
            return None
        from pathlib import Path  # noqa: PLC0415

        dir_path = Path(self.screenshot_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in label)
        path = dir_path / f"{safe}-{int(time.time())}.png"
        with _suppress_exception():
            await self._page.screenshot(path=str(path), full_page=True)
            return str(path)
        return None


# ---------------------------------------------------------------------------
# Pure verdict logic — lives here so the unit tests can drive it without
# Playwright (they construct a `SearchExpectation` + a list of results and
# call `_verdict_for_search` directly).
# ---------------------------------------------------------------------------


def _verdict_for_search(
    expectation: SearchExpectation,
    results: list[SearchResult],
    details: list[str],
) -> CheckOutcome:
    """Validate the observed results against the spec's expectations.

    A spec that expects `every_card_has_cpv` or `cpv_present_for_source`
    is a NORMALISATION check — today, with Proactis missing CPV, the
    bot returns OBSERVED_GAP rather than FAIL. That keeps Phase 0 honest
    without pretending the gap is fixed."""
    sources = {r.source_code for r in results}
    if expectation.min_results is not None and len(results) < expectation.min_results:
        details.append(
            f"min_results: expected >= {expectation.min_results}, got {len(results)}"
        )
        return CheckOutcome.FAIL
    if expectation.max_results is not None and len(results) > expectation.max_results:
        details.append(
            f"max_results: expected <= {expectation.max_results}, got {len(results)}"
        )
        return CheckOutcome.FAIL
    for required in expectation.must_include_sources:
        if required not in sources:
            details.append(
                f"must_include_sources: expected {required}, sources present "
                f"= {sorted(sources)}"
            )
            return CheckOutcome.FAIL
    for forbidden in expectation.must_exclude_sources:
        if forbidden in sources:
            details.append(
                f"must_exclude_sources: forbidden {forbidden} present in "
                f"{sorted(sources)}"
            )
            return CheckOutcome.FAIL
    # Field-presence checks → OBSERVED_GAP when the dashboard genuinely
    # has nothing to show today. We cannot read CPV off the result-card
    # markup itself (SearchPage doesn't surface a CPV cell on the card),
    # so the spec must couple these checks with an `open` step. If neither
    # is set, treat the search verdict as PASS.
    if expectation.every_card_has_cpv is True:
        details.append(
            "every_card_has_cpv: search verdict deferred to an `open` step"
        )
    if expectation.cpv_present_for_source:
        details.append(
            f"cpv_present_for_source({expectation.cpv_present_for_source}): "
            "search verdict deferred to an `open` step"
        )
    return CheckOutcome.PASS


def _friendly_source_label(code: str) -> str:
    """Mirror the dashboard's `sourceLabel(code)` map (lib/format.ts).
    Keeping the small subset here means the bot doesn't need to call the
    dashboard's TS at startup; if a label drifts, the unit test that
    pins both maps catches it."""
    return {
        "FTS": "Find a Tender",
        "CF": "Contracts Finder",
        "PCS": "Public Contracts Scotland",
        "S2W": "Sell2Wales",
        "NI": "eTendersNI",
        "PROACTIS": "Proactis / ProContract",
        "EU_SUPPLY": "EU-Supply / Mercell",
        "ATAMIS": "Atamis",
        "SAMPLE_SEED": "SAMPLE_SEED",
    }.get(code, code)


class _SuppressException:
    """A tiny context manager so we don't pull in contextlib at runtime —
    the bot's failure mode must be "report, don't crash"."""

    def __enter__(self) -> _SuppressException:
        return self

    def __exit__(self, *_: Any) -> bool:
        return True


# Lower-case alias for the call sites: they read like
# `with _suppress_exception():`, which fits the way `contextlib.suppress`
# is invoked. PEP-8 ruling for class names is satisfied by the canonical
# name above.
_suppress_exception = _SuppressException
