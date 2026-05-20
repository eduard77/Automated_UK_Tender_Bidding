#!/usr/bin/env node
// Capture the two platform dashboard screenshots for the PR description.
// Requires the dev/prod server running on :3000 and the backend on :8000.

import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { mkdirSync } from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, "..", "..", "docs", "screenshots");
mkdirSync(OUT_DIR, { recursive: true });

const SHOTS = [
  { path: "/platforms", file: "platforms-list.png" },
  { path: "/platforms/delta_esourcing", file: "platform-delta-detail.png" },
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
