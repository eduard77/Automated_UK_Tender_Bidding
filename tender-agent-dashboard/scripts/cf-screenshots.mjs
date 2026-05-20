#!/usr/bin/env node
// Chunk-3 screenshots: tender detail with documents fetched, the in-progress
// state, and a CF tender showing the "no documents from CF" fallback message.
// Requires dashboard on :3000 and backend on :8000.

import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { mkdirSync } from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, "..", "..", "docs", "screenshots");
mkdirSync(OUT_DIR, { recursive: true });

// 2574 = smoke tender pointing at a real assets.publishing PDF.
// 1422 = a real ingested CF tender (docs are notice/portal links -> empty).
const POPULATED = process.env.CF_TENDER_POPULATED ?? "2574";
const CF_EMPTY = process.env.CF_TENDER_EMPTY ?? "1422";

async function clickFetch(page) {
  const btn = page.getByRole("button", { name: /fetch documents/i });
  await btn.click().catch(() => {});
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1100 } });

    // 1) Populated + in-progress capture.
    const p1 = await ctx.newPage();
    await p1.goto(`http://localhost:3000/tenders/${POPULATED}`, {
      waitUntil: "domcontentloaded",
      timeout: 30000,
    });
    await p1.waitForTimeout(2500);
    await clickFetch(p1);
    // Capture the in-progress banner quickly.
    await p1.waitForTimeout(400);
    await p1.screenshot({
      path: resolve(OUT_DIR, "tender-fetch-in-progress.png"),
      fullPage: true,
    });
    // Let it finish, then capture the populated documents section.
    await p1.waitForTimeout(5000);
    await p1.screenshot({
      path: resolve(OUT_DIR, "tender-documents-populated.png"),
      fullPage: true,
    });
    console.log("captured populated + in-progress");
    await p1.close();

    // 2) Real CF tender -> "no documents from CF" fallback message.
    const p2 = await ctx.newPage();
    await p2.goto(`http://localhost:3000/tenders/${CF_EMPTY}`, {
      waitUntil: "domcontentloaded",
      timeout: 30000,
    });
    await p2.waitForTimeout(2500);
    await clickFetch(p2);
    await p2.waitForTimeout(4000);
    await p2.screenshot({
      path: resolve(OUT_DIR, "tender-cf-no-docs.png"),
      fullPage: true,
    });
    console.log("captured cf-no-docs");
    await p2.close();
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
