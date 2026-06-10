"""Proactis login selectors vs the captured /Login/Index page — offline.

The live login-diagnostic proved the original selectors looked for an EMAIL
field on a page that has a "User Name" field, so the fill timed out and every
run ended `needs_login`. These tests pin the fixed selectors against the
fixture reconstruction of the real page (tests/fixtures/proactis_login_page.html)
so a selector/markup drift fails HERE, not 30s into a cloud run.

No Playwright: a ~60-line evaluator covers exactly the selector grammar the
flow uses (tag, `[attr='v']`, `[attr*='v' i]`, `:has-text('…')`, chained
attribute conditions, comma-separated alternatives) and runs it against the
fixture's parsed elements. The FixtureBridge then makes fill/click/exists
succeed only when a selector genuinely matches the page, so the whole
`login_with_credentials` flow is exercised against the real markup shape.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from tender_agent.services.bridge_client import BridgeError
from tender_agent.services.discovery.proactis_login import (
    PROACTIS_LOGIN_SELECTORS,
    login_with_credentials,
)
from tender_agent.services.portals.base import Credentials
from tests.conftest import load_text_fixture

_CREDS = Credentials(
    username="genera-ops", password="p4ssw0rd", email="ops@genera-systems.com"
)


# --- minimal evaluator for the selector grammar the login flow uses ----------


class _Collector(HTMLParser):
    """Flattens the document into (tag, attrs, text) triples; text is the
    concatenated character data inside the element (any depth)."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[dict] = []
        self._open: list[dict] = []

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.elements.append(el)
        self._open.append(el)

    def handle_endtag(self, tag):
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i]["tag"] == tag:
                del self._open[i]
                break

    def handle_data(self, data):
        for el in self._open:
            el["text"] += data


def _parse_elements(html: str) -> list[dict]:
    collector = _Collector()
    collector.feed(html)
    return collector.elements


_ATTR_RE = re.compile(r"\[([a-zA-Z-]+)(\*?=)'([^']*)'( i)?\]")
_HAS_TEXT_RE = re.compile(r":has-text\('([^']*)'\)")
_CLASS_RE = re.compile(r"\.([a-zA-Z][\w-]*)")


def _alternative_matches(el: dict, alternative: str) -> bool:
    alt = alternative.strip()
    has_texts = _HAS_TEXT_RE.findall(alt)
    alt = _HAS_TEXT_RE.sub("", alt)
    attr_specs = _ATTR_RE.findall(alt)
    alt = _ATTR_RE.sub("", alt)
    classes = _CLASS_RE.findall(alt)
    alt = _CLASS_RE.sub("", alt)
    tag = alt.strip()
    if tag and el["tag"] != tag:
        return False
    el_classes = (el["attrs"].get("class") or "").split()
    if any(c not in el_classes for c in classes):
        return False
    for name, op, value, ci in attr_specs:
        actual = el["attrs"].get(name)
        if actual is None:
            return False
        left, right = (actual.lower(), value.lower()) if ci else (actual, value)
        if op == "=" and left != right:
            return False
        if op == "*=" and right not in left:
            return False
    return all(t.lower() in el["text"].lower() for t in has_texts)


def _selector_hits(elements: list[dict], selector: str) -> list[dict]:
    hits: list[dict] = []
    for alternative in selector.split(","):
        for el in elements:
            if _alternative_matches(el, alternative) and el not in hits:
                hits.append(el)
    return hits


# --- a bridge whose DOM is the fixture ---------------------------------------


class _FixtureBridge:
    """fill/click/element_exists succeed only when the selector matches the
    fixture page, so the flow is driven by the same markup the real browser
    would see. Submitting flips the page to 'post-login' (marker visible,
    non-/Login URL)."""

    def __init__(self, html: str) -> None:
        self.elements = _parse_elements(html)
        self.fills: list[tuple[dict, str]] = []
        self.clicks: list[dict] = []
        self.events: list[str] = []
        self.submitted = False

    async def navigate(self, _slug, url):
        self.events.append("navigate")
        return {"current_url": url, "status_code": 200, "title": "Log In"}

    async def element_exists(self, _slug, selector):
        if self.submitted:
            # Post-submit world: the only probes the flow makes are the
            # logged-in marker and the rejection banner.
            return "validation-summary-errors" not in selector
        return bool(_selector_hits(self.elements, selector))

    async def fill(self, _slug, selector, value):
        hits = _selector_hits(self.elements, selector)
        if not hits:
            raise BridgeError(f"fill failed: timeout waiting for {selector}")
        self.fills.append((hits[0], value))
        self.events.append(f"fill:{hits[0]['attrs'].get('id', '?')}")
        return {"ok": True}

    async def click(self, _slug, selector):
        hits = _selector_hits(self.elements, selector)
        if not hits:
            raise BridgeError(f"click failed: timeout waiting for {selector}")
        self.clicks.append(hits[0])
        attrs = hits[0]["attrs"]
        self.events.append(f"click:{attrs.get('id', attrs.get('class', '?'))}")
        if hits[0]["attrs"].get("type") == "submit":
            self.submitted = True
        return {"ok": True}

    async def session_status(self, _slug):
        if self.submitted:
            return {"current_url": "https://procontract.due-north.com/Home"}
        return {"current_url": "https://procontract.due-north.com/Login/Index"}


# --- selector ↔ fixture contract ---------------------------------------------


def test_username_selector_matches_the_user_name_field():
    elements = _parse_elements(load_text_fixture("proactis_login_page.html"))
    hits = _selector_hits(elements, PROACTIS_LOGIN_SELECTORS["username_input"])
    assert hits, "username selector matched nothing on the captured login page"
    assert hits[0]["attrs"].get("name") == "UserName"
    assert hits[0]["attrs"].get("type") != "email"


def test_password_selector_still_matches():
    elements = _parse_elements(load_text_fixture("proactis_login_page.html"))
    hits = _selector_hits(elements, PROACTIS_LOGIN_SELECTORS["password_input"])
    assert hits and hits[0]["attrs"].get("type") == "password"


def test_submit_selector_matches_the_continue_button():
    elements = _parse_elements(load_text_fixture("proactis_login_page.html"))
    hits = _selector_hits(elements, PROACTIS_LOGIN_SELECTORS["submit_button"])
    assert hits, "submit selector matched nothing on the captured login page"
    assert "Continue" in hits[0]["text"]


def test_cookie_accept_selector_matches_the_banner_button():
    elements = _parse_elements(load_text_fixture("proactis_login_page.html"))
    hits = _selector_hits(elements, PROACTIS_LOGIN_SELECTORS["cookie_accept"])
    assert hits and "Accept all" in hits[0]["text"]
    # And it is NOT the form submit — accepting cookies must not submit.
    assert hits[0]["attrs"].get("type") != "submit"


# --- full flow against the fixture markup -------------------------------------


@pytest.mark.asyncio
async def test_login_flow_drives_the_real_page_shape():
    bridge = _FixtureBridge(load_text_fixture("proactis_login_page.html"))
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    assert attempt.status == "ok"
    # The USERNAME (not the email) went into the UserName field.
    filled = {el["attrs"].get("id"): value for el, value in bridge.fills}
    assert filled["UserName"] == "genera-ops"
    assert filled["Password"] == "p4ssw0rd"
    # Continue was clicked, and the cookie banner was accepted BEFORE any fill.
    assert bridge.submitted is True
    assert bridge.events.index("click:cookie-accept") < bridge.events.index(
        "fill:UserName"
    )


@pytest.mark.asyncio
async def test_banner_absent_page_still_logs_in():
    html = load_text_fixture("proactis_login_page.html")
    start = html.index('<div id="cookieConsent"')
    end = html.index("</div>", start) + len("</div>")
    bridge = _FixtureBridge(html[:start] + html[end:])
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    assert attempt.status == "ok"
    assert not any(e.startswith("click:cookie") for e in bridge.events)


@pytest.mark.asyncio
async def test_page_without_username_field_fails_gracefully():
    """A future redesign that drops/renames the field must still surface the
    clean needs_login diagnostic, not a silent breakage."""
    html = load_text_fixture("proactis_login_page.html")
    html = html.replace(
        '<input id="UserName" name="UserName" type="text" autocomplete="username"',
        '<input id="LoginToken" name="LoginToken" type="text"',
    )
    bridge = _FixtureBridge(html)
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    assert attempt.status == "needs_login"
    assert "username input not found" in (attempt.detail or "")
