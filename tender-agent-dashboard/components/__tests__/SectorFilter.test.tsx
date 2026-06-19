import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

import SearchPage from "@/components/SearchPage";

// Phase 3b: the dashboard sector filter + saved-sectors pre-apply.

const FACETS = {
  sources: ["CF", "FTS"],
  statuses: ["active"],
  regions: [{ value: "London", count: 3 }],
};

const SECTORS = {
  sectors: [
    "Construction & Built Environment",
    "IT & Digital",
    "Health & Social Care",
  ],
};

const SEARCH_RESPONSE = {
  total: 1,
  page: 1,
  page_size: 25,
  results: [
    {
      id: 201,
      title: "Sector Tagged Tender",
      buyer_name: "Mock Council",
      buyer_region: "London",
      region: "London",
      status: "active",
      source_code: "CF",
      source_url: null,
      cpv_codes: null,
      value_amount: "120000",
      value_currency: "GBP",
      value_min: null,
      value_max: null,
      published_at: "2026-05-01T00:00:00Z",
      deadline_at: "2099-01-01T12:00:00Z",
      procurement_ref: "REF-1",
      duplicate_of_id: null,
      primary_sector: "Construction & Built Environment",
      secondary_sectors: ["IT & Digital"],
      is_duplicate: false,
    },
  ],
};

function jsonResponse(data: unknown, ok = true, status = 200): Response {
  return { ok, status, json: async () => data } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;
// Per-test control over what GET /me/sectors returns (drives pre-apply).
let savedSectors: { sectors: string[] };

beforeEach(() => {
  savedSectors = { sectors: [] };
  fetchMock = vi.fn(async (url: string) => {
    const u = String(url);
    if (u.includes("/me/sectors")) return jsonResponse(savedSectors);
    if (u.includes("/tenders/sectors")) return jsonResponse(SECTORS);
    if (u.includes("/tenders/facets")) return jsonResponse(FACETS);
    if (u.includes("/tenders/search")) return jsonResponse(SEARCH_RESPONSE);
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <SearchPage />
    </SWRConfig>,
  );
}

function searchCalls(): string[] {
  return fetchMock.mock.calls
    .map((c) => String(c[0]))
    .filter((u) => u.includes("/tenders/search"));
}

function sectorsLoaded(): boolean {
  return fetchMock.mock.calls.some((c) =>
    String(c[0]).includes("/tenders/sectors"),
  );
}

describe("SearchPage sector filter", () => {
  it("selecting a sector adds it to the query (OR match) and a removable chip", async () => {
    renderPage();
    await waitFor(() => expect(sectorsLoaded()).toBe(true));

    // The 16 sectors come from the backend taxonomy, rendered as toggles.
    fireEvent.click(screen.getByRole("button", { name: "IT & Digital" }));
    fireEvent.click(screen.getByRole("button", { name: /^search/i }));

    await waitFor(() =>
      expect(searchCalls().some((u) => u.includes("sector=IT"))).toBe(true),
    );

    // Active-filter chip is shown; removing it re-queries without the sector.
    const remove = await screen.findByRole("button", {
      name: /Remove filter Sector/i,
    });
    fireEvent.click(remove);
    await waitFor(() =>
      expect(searchCalls().some((u) => !u.includes("sector="))).toBe(true),
    );
  });

  it("renders the primary-sector badge on each result", async () => {
    renderPage();
    await waitFor(() => expect(sectorsLoaded()).toBe(true));

    fireEvent.click(screen.getByRole("button", { name: /^search/i }));

    await waitFor(() =>
      expect(screen.getByText("Sector Tagged Tender")).toBeInTheDocument(),
    );
    // The badge (unique title) shows why the tender matched.
    expect(
      screen.getByTitle("Primary sector: Construction & Built Environment"),
    ).toBeInTheDocument();
  });

  it("pre-applies the user's SAVED sectors on load without pressing Search", async () => {
    savedSectors = { sectors: ["IT & Digital"] };
    renderPage();

    // A search fires on mount, scoped to the saved sector.
    await waitFor(() =>
      expect(searchCalls().some((u) => u.includes("sector=IT"))).toBe(true),
    );
    await waitFor(() =>
      expect(screen.getByText("Sector Tagged Tender")).toBeInTheDocument(),
    );
  });

  it("does NOT pre-apply when the user has no saved sectors", async () => {
    savedSectors = { sectors: [] };
    renderPage();
    await waitFor(() => expect(sectorsLoaded()).toBe(true));
    // Give the mount effect a tick to resolve; still no auto-search.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(searchCalls()).toHaveLength(0);
    expect(screen.getByText(/Set your filters above/i)).toBeInTheDocument();
  });
});
