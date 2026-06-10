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
    """Flattens the document into (tag, attrs, text, ancestors) tuples; text
    is the concatenated character data inside the element (any depth), and
    `ancestors` references the chain of open elements above this one so
    descendant combinators can be evaluated cheaply."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[dict] = []
        self._open: list[dict] = []

    def handle_starttag(self, tag, attrs):
        el = {
            "tag": tag,
            "attrs": dict(attrs),
            "text": "",
            "ancestors": list(self._open),
        }
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


def _compound_matches(el: dict, compound: str) -> bool:
    """One whitespace-separated piece of a selector (tag + classes + attrs +
    :has-text), matched against a single element. The descendant combinator
    is handled one level up in `_selector_hits`."""
    alt = compound.strip()
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


def _split_compounds(alternative: str) -> list[str]:
    """Split on whitespace, but keep `:has-text('… …')` arguments together."""
    out: list[str] = []
    buf = ""
    depth = 0
    for ch in alternative.strip():
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            if buf:
                out.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def _selector_hits(elements: list[dict], selector: str) -> list[dict]:
    hits: list[dict] = []
    for alternative in selector.split(","):
        compounds = _split_compounds(alternative)
        if not compounds:
            continue
        for el in elements:
            if not _compound_matches(el, compounds[-1]):
                continue
            # Walk ancestors right-to-left through the remaining compounds.
            ancestor_chain = list(el["ancestors"])
            ok = True
            for parent_compound in reversed(compounds[:-1]):
                while ancestor_chain and not _compound_matches(
                    ancestor_chain[-1], parent_compound
                ):
                    ancestor_chain.pop()
                if not ancestor_chain:
                    ok = False
                    break
                ancestor_chain.pop()
            if ok and el not in hits:
                hits.append(el)
    return hits


# --- a bridge whose DOM is the fixture ---------------------------------------


class _FixtureBridge:
    """fill/click/element_exists succeed only when the selector matches the
    fixture page, so the flow is driven by the same markup the real browser
    would see. Submitting flips the page to 'post-login' (marker visible,
    non-/Login URL).

    Two pieces of mutable state model the real browser's behaviour: the
    cookie dialog detaches once its Accept button is clicked, and clicking
    the form's submit button (`type=submit`) is INTERCEPTED while the cookie
    dialog is still present — exactly the live failure."""

    def __init__(self, html: str) -> None:
        self.elements = _parse_elements(html)
        self.fills: list[tuple[dict, str]] = []
        self.clicks: list[dict] = []
        self.events: list[str] = []
        self.submitted = False
        # Track the cookie dialog's elements separately so dismissal removes
        # them from `element_exists` results.
        self._cookie_dialog_elements = {
            id(el)
            for el in self.elements
            if "js-cookie-consent-dialog" in (el["attrs"].get("class") or "")
            or el["tag"] == "h2"
            and el["attrs"].get("id") == "cookie-consent-dialog-title"
        }
        # Element ids inside the dialog subtree — clicking any of them counts
        # as a dismiss event for the Accept button.
        self._cookie_subtree_ids = {
            id(el)
            for el in self.elements
            if any(
                id(a) in self._cookie_dialog_elements for a in el["ancestors"]
            )
            or id(el) in self._cookie_dialog_elements
        }
        self.cookie_dialog_present = bool(self._cookie_dialog_elements)

    async def navigate(self, _slug, url):
        self.events.append("navigate")
        return {"current_url": url, "status_code": 200, "title": "Log In"}

    def _visible_elements(self) -> list[dict]:
        if not self.cookie_dialog_present:
            return [
                el
                for el in self.elements
                if id(el) not in self._cookie_subtree_ids
            ]
        return self.elements

    async def element_exists(self, _slug, selector):
        if self.submitted:
            # Post-submit world: the only probes the flow makes are the
            # logged-in marker and the rejection banner.
            return "validation-summary-errors" not in selector
        return bool(_selector_hits(self._visible_elements(), selector))

    async def fill(self, _slug, selector, value):
        hits = _selector_hits(self._visible_elements(), selector)
        if not hits:
            raise BridgeError(f"fill failed: timeout waiting for {selector}")
        self.fills.append((hits[0], value))
        self.events.append(f"fill:{hits[0]['attrs'].get('id', '?')}")
        return {"ok": True}

    async def click(self, _slug, selector):
        hits = _selector_hits(self._visible_elements(), selector)
        if not hits:
            raise BridgeError(f"click failed: timeout waiting for {selector}")
        clicked = hits[0]
        # Real Playwright behaviour: a click on the form submit is INTERCEPTED
        # by the cookie dialog while it's still on top — the timeout the user
        # saw 42× in the diagnostic.
        if (
            clicked["attrs"].get("type") == "submit"
            and clicked["tag"] in {"button", "input"}
            and self.cookie_dialog_present
        ):
            raise BridgeError(
                "click failed: <button class='js-cookie-consent-dialog'> "
                "intercepts pointer events"
            )
        self.clicks.append(clicked)
        attrs = clicked["attrs"]
        self.events.append(f"click:{attrs.get('id', attrs.get('class', '?'))}")
        if id(clicked) in self._cookie_subtree_ids:
            # The dialog accept (or any in-dialog button) closes the dialog.
            self.cookie_dialog_present = False
        if clicked["attrs"].get("type") == "submit" and clicked["tag"] in {
            "button",
            "input",
        }:
            self.submitted = True
        return {"ok": True}

    async def session_status(self, _slug):
        if self.submitted:
            return {"current_url": "https://procontract.due-north.com/Home"}
        return {"current_url": "https://procontract.due-north.com/Login/Index"}

    # --- robust-dismissal primitives (mirrors InProcessBridgeClient) -------
    # Config flags let a test choose which strategy the routine lands on:
    #   removal_works   — does the JS overlay-detach actually clear it?
    #   force_click_works — does the forced submit click get through?
    # Defaults model the happy path (text-accept clears it).

    removal_works = True
    force_click_works = True

    async def click_by_text(self, _slug, scope_selector, text, *, timeout_ms=2000):
        # Only the cookie dialog is in scope here; match a CLICKABLE element
        # (button/a/input) inside the dialog subtree whose text contains the
        # target (case-insensitive). A paragraph that merely mentions "Accept
        # all" is never clicked.
        if not self.cookie_dialog_present:
            return False
        for el in self.elements:
            if (
                id(el) in self._cookie_subtree_ids
                and el["tag"] in {"button", "a", "input"}
                and text.lower() in el["text"].lower()
            ):
                self.events.append(f"click_by_text:{text}")
                self.cookie_dialog_present = False
                return True
        return False

    async def evaluate(self, _slug, script):
        if "requestSubmit" in script:
            self.events.append("evaluate:submit_form")
            self.submitted = True
            return True
        # Overlay-removal snippet.
        self.events.append("evaluate:remove_overlay")
        if self.removal_works:
            self.cookie_dialog_present = False
            return 1
        return 0

    async def force_click(self, _slug, selector):
        hits = _selector_hits(self._visible_elements(), selector)
        if not hits:
            raise BridgeError("force click failed: no match")
        if not self.force_click_works:
            raise BridgeError("force click failed: still intercepted")
        clicked = hits[0]
        self.events.append(f"force_click:{clicked['attrs'].get('id', '?')}")
        if clicked["attrs"].get("type") == "submit":
            self.submitted = True
        return {"ok": True}


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


def test_cookie_accept_selector_is_text_based_and_dialog_scoped():
    """The accept selector is markup-agnostic (Playwright text engine) and
    every alternative is scoped to a cookie-dialog container — never a bare
    page-wide text match that could hit something unrelated."""
    sel = PROACTIS_LOGIN_SELECTORS["cookie_accept"]
    assert "text=" in sel  # matches by visible text, not class/tag
    for alternative in sel.split(","):
        assert ">>" in alternative  # scoped: <container> >> text=...
        assert "cookie" in alternative.lower()


# --- full flow against the fixture markup -------------------------------------


@pytest.mark.asyncio
async def test_login_flow_drives_the_real_page_shape():
    """Baseline /Login/Index — no consent dialog in the way. The login
    drives the form straight through; the dismissal step is opt-in and is
    not exercised here. The with-dialog scenario lives in its own test."""
    bridge = _FixtureBridge(load_text_fixture("proactis_login_page.html"))
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    assert attempt.status == "ok"
    # The USERNAME (not the email) went into the UserName field.
    filled = {el["attrs"].get("id"): value for el, value in bridge.fills}
    assert filled["UserName"] == "genera-ops"
    assert filled["Password"] == "p4ssw0rd"
    assert bridge.submitted is True
    # No dialog → no cookie dismiss attempt, telemetry stays None.
    assert attempt.cookie_dialog_dismissed is None


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
    assert attempt.cookie_dialog_dismissed is None
    assert not any("Accept" in e or "cookie" in e.lower() for e in bridge.events)


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


# --- cookie consent DIALOG contract (the live PR #109 follow-up failure) -----


def test_cookie_dialog_selector_hits_the_real_dialog_container():
    elements = _parse_elements(
        load_text_fixture("proactis_login_page_with_cookie_dialog.html")
    )
    hits = _selector_hits(elements, PROACTIS_LOGIN_SELECTORS["cookie_dialog"])
    assert hits, "cookie_dialog selector matched no element on the captured page"
    assert "js-cookie-consent-dialog" in (hits[0]["attrs"].get("class") or "")
    assert hits[0]["attrs"].get("role") == "dialog"


@pytest.mark.asyncio
async def test_strategy1_text_accept_dismisses_before_submit():
    """The full chain from the third live diagnostic: dialog present →
    clicked by VISIBLE TEXT ("Accept all") → cleared → Continue clicks.
    This is the ordering the live failure violated."""
    bridge = _FixtureBridge(
        load_text_fixture("proactis_login_page_with_cookie_dialog.html")
    )
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    assert attempt.status == "ok"
    assert attempt.cookie_dialog_dismissed is True
    assert attempt.cookie_dismiss_method == "text-accept"
    accept_idx = next(
        i for i, e in enumerate(bridge.events) if e.startswith("click_by_text")
    )
    fill_idx = bridge.events.index("fill:UserName")
    submit_idx = bridge.events.index("click:continueButton")
    assert accept_idx < fill_idx < submit_idx
    assert bridge.submitted is True
    # Strategy 1 worked → no overlay removal, no forced submit needed.
    assert not any(e.startswith("evaluate") for e in bridge.events)
    assert not any(e.startswith("force_click") for e in bridge.events)


@pytest.mark.asyncio
async def test_strategy3_removes_overlay_when_accept_text_unmatchable():
    """Accept control text we can't match → routine falls through to
    detaching the overlay via JS (strategy 3) and still logs in."""
    html = load_text_fixture("proactis_login_page_with_cookie_dialog.html")
    # Rename the button text so click_by_text can't find any accept variant;
    # the <p> still mentions "Accept all" but a paragraph is never clicked.
    html = html.replace(">Accept all</button>", ">Allow selected</button>")
    bridge = _FixtureBridge(html)
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    assert attempt.status == "ok"
    assert attempt.cookie_dialog_dismissed is True
    assert attempt.cookie_dismiss_method == "removed"
    assert "evaluate:remove_overlay" in bridge.events
    # Overlay gone before the Continue click landed.
    assert bridge.events.index("evaluate:remove_overlay") < bridge.events.index(
        "click:continueButton"
    )


@pytest.mark.asyncio
async def test_strategy5_forced_submit_when_overlay_cannot_be_cleared(monkeypatch):
    """Residual overlay at submit time: text-accept misses AND removal fails →
    the submit click is intercepted → forced submit gets login through."""
    import tender_agent.services.discovery.proactis_login as pl

    # The overlay never clears here, so don't burn the full dismiss timeout.
    monkeypatch.setattr(pl, "COOKIE_DIALOG_DISMISS_TIMEOUT_S", 0.05)
    html = load_text_fixture("proactis_login_page_with_cookie_dialog.html")
    html = html.replace(">Accept all</button>", ">Allow selected</button>")
    bridge = _FixtureBridge(html)
    bridge.removal_works = False  # JS detach is a no-op → dialog stays up
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    assert attempt.status == "ok"
    assert attempt.cookie_dismiss_method == "forced-submit"
    # A normal submit click was attempted and intercepted; the forced click
    # then carried it through.
    assert any(e.startswith("force_click") for e in bridge.events)
    assert bridge.submitted is True


@pytest.mark.asyncio
async def test_login_proceeds_when_no_dialog_present():
    """Banner-absent variant must still log in with no error and no clicks
    against any cookie selector — the dismissal step is opt-in."""
    bridge = _FixtureBridge(load_text_fixture("proactis_login_page.html"))
    bridge.cookie_dialog_present = False  # banner absent on this fixture
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    assert attempt.status == "ok"
    assert attempt.cookie_dialog_dismissed is None
    assert attempt.cookie_dismiss_method is None
    assert not any(
        e.startswith("click_by_text") or e.startswith("evaluate")
        for e in bridge.events
    )


@pytest.mark.asyncio
async def test_diagnostic_reports_cookie_dialog_dismissed_true():
    from tender_agent.services.discovery.proactis_login_diagnostic import (
        capture_login_state,
    )

    bridge = _FixtureBridge(
        load_text_fixture("proactis_login_page_with_cookie_dialog.html")
    )

    # The diagnostic uses page_text / screenshot / session_status — add the
    # minimal extra surface to the fixture bridge for this one test.
    async def _page_text(_slug):
        return "Welcome to ProContract. User Name. Password. Continue."

    async def _screenshot(_slug, label="screenshot"):
        return {"path": f"{label}.png", "size_bytes": 0}

    bridge.page_text = _page_text  # type: ignore[attr-defined]
    bridge.screenshot = _screenshot  # type: ignore[attr-defined]
    attempt = await login_with_credentials(
        bridge, slug="procontract", credentials=_CREDS
    )
    diag = await capture_login_state(bridge, "procontract", attempt=attempt)
    assert diag.cookie_dialog_dismissed is True
    assert diag.cookie_dismiss_method == "text-accept"
    assert diag.logged_in is True
    # The method also rides through to the JSON the admin endpoint returns.
    assert diag.as_dict()["cookie_dismiss_method"] == "text-accept"
