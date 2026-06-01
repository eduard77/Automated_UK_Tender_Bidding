/**
 * Brief-locking behaviour. The server is the source of truth for whether a
 * brief is locked (it strips the JSON and sets `brief_json.locked=true`
 * for anonymous/unentitled callers). These tests verify the front-end
 * branches on that flag correctly:
 *
 *   - locked=true  → the half-preview panel + UnlockOverlay are shown.
 *   - locked absent → the existing full RecommendationBanner renders.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

import TenderDetail from "@/components/TenderDetail";

function jsonResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  } as unknown as Response;
}

const BASE_TENDER = {
  id: 42,
  source_code: "CF",
  source_ref: "CF-42",
  source_url: null,
  title: "Refurbishment of Library Reading Rooms",
  description: "Refurbishment works...",
  notice_type: "contract",
  status: "active",
  buyer_name: "Birmingham City Council",
  buyer_country: "United Kingdom",
  buyer_region: "West Midlands",
  region: "West Midlands",
  cpv_codes: ["45454000"],
  keywords: [],
  value_amount: "450000",
  value_currency: "GBP",
  value_min: null,
  value_max: null,
  published_at: "2026-05-01T00:00:00Z",
  deadline_at: "2099-01-01T12:00:00Z",
  procurement_ref: null,
  duplicate_of_id: null,
  is_duplicate: false,
};

const LOCKED_BRIEF = {
  id: 1,
  tender_id: 42,
  status: "complete",
  recommendation: null,
  confidence: null,
  headline: "Mid-size West Midlands refurbishment with a tight autumn window.",
  brief_json: {
    locked: true,
    headline: "Mid-size West Midlands refurbishment with a tight autumn window.",
    scope_summary:
      "Three reading rooms refurbished to a current accessibility standard.",
    counts: {
      key_risks_count: 3,
      mandatory_requirements_count: 6,
      scoring_criteria_count: 5,
    },
    unlock: { reason: "brief_locked", message: "Full brief is locked." },
  },
  model: null,
  documents_considered: null,
  input_tokens: null,
  output_tokens: null,
  error_detail: null,
  generated_at: "2026-06-01T00:00:00Z",
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
};

const UNLOCKED_BRIEF = {
  id: 2,
  tender_id: 42,
  status: "complete",
  recommendation: "bid",
  confidence: "medium",
  headline: "Full brief headline",
  brief_json: {
    recommendation: "bid",
    confidence: "medium",
    headline: "Full brief headline",
    rationale: "Strong fit; we should bid.",
    key_risks: [
      { risk: "Programme risk", severity: "high", detail: "Tight window." },
    ],
    deadline: { date: "2099-01-01", note: null },
    contract_value: { amount: "£450,000", note: null },
    mandatory_requirements: ["ISO 9001"],
    scoring: { summary: "60/40 quality/price", criteria: [] },
    scope_summary: "Full scope.",
    notable_conditions: [],
    missing_or_unclear: [],
  },
  model: "claude-sonnet-4-5",
  documents_considered: [],
  input_tokens: 1000,
  output_tokens: 500,
  error_detail: null,
  generated_at: "2026-06-01T00:00:00Z",
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
};

let fetchMock: ReturnType<typeof vi.fn>;

function setupFetch(brief: unknown) {
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/me")) return jsonResponse(null, 401);
    if (url.includes("/billing/status")) {
      return jsonResponse({
        payments_configured: true,
        publishable_key: "pk_test_xxx",
        plan_prices: {
          payg_brief: "price_brief",
          plan_100: "price_plan_100",
          plan_unlimited: "price_unlim",
        },
      });
    }
    if (url.includes("/billing/submission-fee/")) {
      return jsonResponse({
        tender_id: 42,
        amount_pence: 22500,
        amount_gbp: 225,
        currency: "GBP",
        payments_configured: true,
        already_paid: false,
      });
    }
    if (url.endsWith(`/tenders/42/brief`)) return jsonResponse(brief);
    if (url.endsWith(`/tenders/42/documents`)) return jsonResponse([]);
    if (url.endsWith(`/tenders/42`)) return jsonResponse(BASE_TENDER);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => {
  // Provide a default URL for relative fetches in jsdom.
  if (!process.env.NEXT_PUBLIC_API_BASE_URL) {
    process.env.NEXT_PUBLIC_API_BASE_URL = "http://localhost:8000";
  }
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderDetail() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <TenderDetail id={42} />
    </SWRConfig>,
  );
}

describe("TenderDetail brief gating", () => {
  it("renders the locked half-preview and unlock CTAs when brief_json.locked is true", async () => {
    setupFetch(LOCKED_BRIEF);
    renderDetail();

    // The preview pill should be visible.
    await waitFor(() =>
      expect(screen.getByText(/First half · preview/i)).toBeInTheDocument(),
    );

    // The allow-listed headline survives.
    expect(
      screen.getByText(/Mid-size West Midlands refurbishment/i),
    ).toBeInTheDocument();

    // Counts block — backend-provided numbers, not derived client-side.
    expect(screen.getByText(/Key risks identified/i)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();

    // The three plan buttons from UnlockOverlay are rendered with prices
    // (payments_configured = true in the mock).
    await waitFor(() =>
      expect(screen.getByText(/Single brief — £10/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Plan 100 — £100\/mo/i)).toBeInTheDocument();
    expect(screen.getByText(/Unlimited — £250\/mo/i)).toBeInTheDocument();

    // The submission-package fee is surfaced from /billing/submission-fee.
    await waitFor(() =>
      expect(screen.getByText(/Generate package — £225/)).toBeInTheDocument(),
    );

    // CRITICAL: the full-brief RecommendationBanner must NOT render.
    expect(screen.queryByText(/^BID$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/medium confidence/i)).not.toBeInTheDocument();
  });

  it("renders the full RecommendationBanner when locked is absent", async () => {
    setupFetch(UNLOCKED_BRIEF);
    renderDetail();

    await waitFor(() => expect(screen.getByText("BID")).toBeInTheDocument());
    expect(screen.getByText(/medium confidence/i)).toBeInTheDocument();
    expect(screen.getByText("Programme risk")).toBeInTheDocument();

    // Lock UI must NOT appear.
    expect(screen.queryByText(/First half · preview/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Single brief — £10/i)).not.toBeInTheDocument();
  });
});
