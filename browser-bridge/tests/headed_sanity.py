#!/usr/bin/env python3
"""Headed sanity check: open the practice site in a VISIBLE window and
screenshot it, proving the visible-window path works on this machine's Chrome.

    python tests/headed_sanity.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "docs" / "screenshots" / "bridge-headed-window.png"


async def main() -> int:
    from playwright.async_api import async_playwright

    OUT.parent.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        str(Path.home() / ".tender-agent" / "bridge-sessions" / "headed-sanity"),
        headless=False,
        args=["--no-first-run", "--no-default-browser-check"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://the-internet.herokuapp.com/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    await page.screenshot(path=str(OUT))
    print(f"Headed window screenshot saved: {OUT}")
    await context.close()
    await pw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
