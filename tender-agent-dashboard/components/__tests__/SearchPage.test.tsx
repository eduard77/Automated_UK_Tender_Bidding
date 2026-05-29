import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

import SearchPage from "@/components/SearchPage";

const FACETS = {
  sources: ["CF", "FTS", "PCS"],
  statuses: ["active", "closed"],
  // Chunk 8: canonical regions present, with counts (drives the dropdown).
  regions: [
    { value: "London", count: 12 },
    { value: "South West", count: 4 },
  ],
};

const SEARCH_RESPONSE = {
  total: 30, // > PAGE_SIZE (25) so pagination renders
  page: 1,
  page_size: 25,
  results: [
    {
      id: 101,
      title: "Mock Result Tender",
      buyer_name: "Mock City Council",
      buyer_region: "London",
      region: "London",
      status: "active",
      source_code: "CF",
      source_url: null,
      cpv_codes: ["45000000"],
      value_amount: "250000",
      value_currency: "GBP",
      value_min: null,
      value_max: null,
      published_at: "2026-05-01T00:00:00Z",
      deadline_at: "2099-01-01T12:00:00Z",
      procurement_ref: "ABC-1",
      duplicate_of_id: 5,
      is_duplicate: true,
    },
  ],
};

function jsonResponse(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => data,
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (url: string) => {
    const u = String(url);
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
  // Fresh SWR cache per render so state never leaks between tests.
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

describe("SearchPage", () => {
  it("renders the filter panel and does not query until Search is pressed", async () => {
    renderPage();

    expect(screen.getByText("Find any tender.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /search/i })).toBeInTheDocument();
    // Prompt state, no results yet.
    expect(screen.getByText(/Set your filters above/i)).toBeInTheDocument();

    // Facets load on mount, but the search endpoint is NOT hit (click-to-search).
    await waitFor(() => expect(facetsLoaded()).toBe(true));
    expect(searchCalls()).toHaveLength(0);
  });

  it("triggers the search query on click and renders results with source badge + duplicate chip", async () => {
    renderPage();
    await waitFor(() => expect(facetsLoaded()).toBe(true));

    fireEvent.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() => expect(screen.getByText("Mock Result Tender")).toBeInTheDocument());
    expect(searchCalls().length).toBeGreaterThan(0);

    // Source badge on the result row (scoped by title to avoid the matching
    // source filter chip) + duplicate marking.
    expect(screen.getByTitle("CF")).toHaveTextContent("Contracts Finder");
    // Duplicate chip links to the primary listing (#5).
    const dupChip = screen.getByRole("link", { name: /Duplicate/i });
    expect(dupChip).toHaveAttribute("href", "/tenders/5");
    // Result count.
    expect(screen.getByText(/30 tenders match/i)).toBeInTheDocument();
  });

  it("paginates: Next requests page 2", async () => {
    renderPage();
    await waitFor(() => expect(facetsLoaded()).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: /search/i }));
    await waitFor(() => expect(screen.getByText("Mock Result Tender")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /next/i }));

    await waitFor(() =>
      expect(searchCalls().some((u) => u.includes("page=2"))).toBe(true),
    );
  });

  it("region multi-select adds an exact region to the query + a removable chip", async () => {
    renderPage();
    await waitFor(() => expect(facetsLoaded()).toBe(true));

    // Canonical region options come from the facets dropdown (not free text).
    fireEvent.click(screen.getByRole("checkbox", { name: /London/i }));
    fireEvent.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() =>
      expect(searchCalls().some((u) => u.includes("region=London"))).toBe(true),
    );

    // An active-filter chip is shown; removing it re-queries without the region.
    const remove = await screen.findByRole("button", { name: /Remove filter Region/i });
    fireEvent.click(remove);
    await waitFor(() =>
      expect(searchCalls().some((u) => !u.includes("region=London"))).toBe(true),
    );
  });

  it("value range includes null-value tenders by default; the toggle flips it", async () => {
    renderPage();
    await waitFor(() => expect(facetsLoaded()).toBe(true));

    fireEvent.change(screen.getByPlaceholderText("50000"), {
      target: { value: "100000" },
    });
    fireEvent.click(screen.getByRole("button", { name: /search/i }));
    await waitFor(() =>
      expect(searchCalls().some((u) => u.includes("value_min=100000"))).toBe(true),
    );
    // Default: null-value tenders are NOT excluded — no value_stated_only param.
    expect(searchCalls().every((u) => !u.includes("value_stated_only"))).toBe(true);

    // Flip the toggle on → the exclusion is requested.
    fireEvent.click(
      screen.getByRole("checkbox", { name: /Only tenders with a stated value/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: /search/i }));
    await waitFor(() =>
      expect(searchCalls().some((u) => u.includes("value_stated_only=true"))).toBe(true),
    );
  });

  it("Clear resets back to the prompt state", async () => {
    renderPage();
    await waitFor(() => expect(facetsLoaded()).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: /search/i }));
    await waitFor(() => expect(screen.getByText("Mock Result Tender")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /clear/i }));

    expect(screen.queryByText("Mock Result Tender")).not.toBeInTheDocument();
    expect(screen.getByText(/Set your filters above/i)).toBeInTheDocument();
  });
});

function facetsLoaded(): boolean {
  return fetchMock.mock.calls.some((c) => String(c[0]).includes("/tenders/facets"));
}
