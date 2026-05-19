#!/usr/bin/env node
// Capture the three dashboard screenshots for the PR description.
// Each path is loaded with the dev server already running on :3000.

import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { mkdirSync } from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, "..", "..", "docs", "screenshots");
mkdirSync(OUT_DIR, { recursive: true });

const SHOTS = [
  { path: "/portals", file: "portals-list.png" },
  { path: "/portals/50", file: "portal-detail.png" },
  { path: "/portals/blocklist", file: "portals-blocklist.png" },
];

async function main() {
  const browser = await chromium.launch({ headless: true });
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 960 } });
    for (const shot of SHOTS) {
      const page = await ctx.newPage();
      await page.goto(`http://localhost:3000${shot.path}`, {
        waitUntil: "domcontentloaded",
        timeout: 30_000,
      });
      // Wait long enough for SWR to fetch and the UI to paint with data.
      await page.waitForTimeout(3500);
      const file = resolve(OUT_DIR, shot.file);
      await page.screenshot({ path: file, fullPage: true });
      console.log(`captured ${shot.path} -> ${file}`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
