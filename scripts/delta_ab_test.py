#!/usr/bin/env python3
"""Delta 403 A/B probe — is the cloud refusal IP-based or fingerprint-based?

ONE-OFF DIAGNOSTIC. Not product code, imports nothing outside the stdlib, and
changes nothing. It replays an already-captured Delta session cookie against
`/delta/mainMenu.html` and reports what Delta sends back, so the SAME cookie can
be fired from two different source IPs (your laptop's residential IP and the
Azure datacenter IP) with the source IP as the ONLY variable between runs.

How to read the result
----------------------
* laptop -> 200 + real menu, Azure -> 403  => the block is IP-BASED (datacenter
  IP reputation and/or Delta binding the session to the login IP).
* both -> 200                              => cookie is fine from a plain HTTP
  client; the cloud 403 is then specific to our HEADLESS BROWSER fingerprint
  (HeadlessChrome UA etc.), not the IP — compare against the in-app probe.
* both -> 403                              => the cookie is rejected regardless
  of IP (expired / single-session invalidated / a non-browser-client block).
* either -> 302 to a /login page           => that side is simply not logged in
  (a clean unauthenticated redirect, NOT the 403 we're chasing).

Security
--------
The storage_state.json is a CREDENTIAL. This script reads the session cookie(s)
to SEND them to Delta, but it NEVER prints a cookie value — only names + counts.
It dumps no full body. Delete any copy you move around when you're done.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

DELTA_URL = "https://www.delta-esourcing.com/delta/mainMenu.html"
DELTA_DOMAIN = "delta-esourcing.com"

# The default location of the operator-uploaded session inside the Azure App
# Service container, so on Azure you can run this with no arguments.
CLOUD_STATE_PATH = "/data/bridge-sessions/delta_esourcing/storage_state.json"

# A normal desktop Chrome UA — deliberately NOT HeadlessChrome — so this run
# differs from the in-app headless browser only in the browser fingerprint,
# letting us separate "IP block" from "headless fingerprint block".
DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# A session cookie is JSESSIONID (Delta is a Java servlet app) or anything else
# with "session" in its name. We send EVERY delta-esourcing.com cookie (a
# browser would) for a faithful replay, and report the session-cookie names.
SESSION_COOKIE_NAMES = ("JSESSIONID",)

# Response headers worth seeing — the WAF/CDN/origin signature. Exact names plus
# prefixes (so any X-Akamai-* / X-Sucuri-* variant is captured).
HEADER_EXACT = (
    "server", "cf-ray", "cf-mitigated", "x-iinfo", "retry-after",
    "via", "x-cdn", "x-cache", "content-type", "location",
)
HEADER_PREFIXES = ("x-akamai", "x-sucuri", "x-amz-cf", "x-waf")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def _is_session_name(name: str) -> bool:
    return name in SESSION_COOKIE_NAMES or "session" in name.lower()


def load_delta_cookies(path: Path) -> tuple[str, list[str], int]:
    """Return (cookie_header, session_cookie_names, total_delta_cookies).

    Reads the Playwright storage_state, keeps every cookie on the
    delta-esourcing.com domain, builds a Cookie request header from them, and
    separately lists the session-cookie NAMES (values are never returned/printed).
    """
    try:
        # utf-8-sig tolerates a stray BOM if the file was copied via a Windows
        # editor; a real Playwright storage_state has none.
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        sys.exit(f"ERROR: no storage_state at {path}")
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"ERROR: cannot read storage_state at {path}: {exc}")

    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list):
        sys.exit("ERROR: storage_state has no 'cookies' list")

    pairs: list[str] = []
    session_names: list[str] = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain", "")).lower()
        name = str(c.get("name", ""))
        if DELTA_DOMAIN not in domain or not name or "value" not in c:
            continue
        pairs.append(f"{name}={c['value']}")
        if _is_session_name(name):
            session_names.append(name)
    if not pairs:
        sys.exit("ERROR: no delta-esourcing.com cookies found in storage_state")
    return "; ".join(pairs), sorted(set(session_names)), len(pairs)


def egress_ip() -> str:
    """Best-effort public IP of THIS host, so the two runs visibly differ by IP.
    Never fatal — returns '?' if the lookup is blocked."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                return r.read().decode("utf-8", "replace").strip()[:64]
        except Exception:  # noqa: BLE001
            continue
    return "?"


def fetch(cookie_header: str) -> tuple[str, int, dict, bytes]:
    """GET mainMenu with the cookies + a desktop UA, following redirects.
    Returns (final_url, status, headers_lower_dict, body). A 4xx/5xx still
    returns its response (caught), so we can read the 403 page + its headers."""
    req = urllib.request.Request(
        DELTA_URL,
        headers={
            "User-Agent": DESKTOP_CHROME_UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "identity",  # avoid gzip so we can read the body
            "Upgrade-Insecure-Requests": "1",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:  # noqa: S310
            return r.geturl(), r.status, dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:  # the 403/4xx/5xx path
        body = b""
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            pass
        return e.url or DELTA_URL, e.code, dict(e.headers.items()), body
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: request failed (network/TLS): {e.reason}")


def summarise_body(body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    m = _TITLE_RE.search(text)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        if title:
            return f"<title> {title}"
    stripped = re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()
    return "body[:200] " + (stripped[:200] or "(empty)")


def picked_headers(headers: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    set_cookie_names: list[str] = []
    for k, v in headers.items():
        kl = k.lower()
        if kl == "set-cookie":
            # Names only — a fresh Set-Cookie here often means "we look logged out".
            set_cookie_names.append(v.split("=", 1)[0].strip())
            continue
        if kl in HEADER_EXACT or any(kl.startswith(p) for p in HEADER_PREFIXES):
            out.append((k, v))
    if set_cookie_names:
        out.append(("set-cookie (names only)", ", ".join(sorted(set(set_cookie_names)))))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="delta_ab_test",
        description="Replay a Delta session cookie at mainMenu.html and report "
        "status/headers/body — run from two IPs to A/B the 403.",
    )
    ap.add_argument(
        "state_path", nargs="?", default=CLOUD_STATE_PATH,
        help=f"Path to storage_state.json (default: {CLOUD_STATE_PATH}).",
    )
    ap.add_argument(
        "--no-ip", action="store_true",
        help="Skip the public-IP lookup (don't print this host's egress IP).",
    )
    args = ap.parse_args(argv)

    cookie_header, session_names, total = load_delta_cookies(Path(args.state_path))
    ip = "(skipped)" if args.no_ip else egress_ip()
    final_url, status, headers, body = fetch(cookie_header)

    print("=" * 64)
    print(" Delta 403 A/B probe")
    print("=" * 64)
    print(f" source egress IP : {ip}")
    print(f" cookies sent     : {total} delta-esourcing.com cookie(s)")
    print(f" session cookies  : {', '.join(session_names) or '(none found!)'}")
    print(" request UA       : desktop Chrome (NOT HeadlessChrome)")
    print("-" * 64)
    print(f" final URL        : {final_url}")
    print(f" HTTP status      : {status}")
    print(f" body / title     : {summarise_body(body)}")
    print(" key headers      :")
    picked = picked_headers(headers)
    if not picked:
        print("   (none of the watched WAF/CDN/origin headers were present)")
    for k, v in picked:
        print(f"   {k}: {v}")
    print("=" * 64)
    low = (summarise_body(body) + " " + str(status)).lower()
    if status == 403:
        print(" => 403: session refused at this IP/fingerprint (the bug).")
    elif "login" in final_url.lower() or "login" in low:
        print(" => redirected to login: this side is simply NOT logged in.")
    elif status == 200:
        print(" => 200: session ACCEPTED from this IP. Compare with the other IP.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
