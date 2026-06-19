import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

import SetupPage from "@/components/SetupPage";
import { AuthProvider } from "@/lib/auth";

// Phase 3b setup page: load a user's saved sectors, edit, persist per-user.

const SECTORS = {
  sectors: [
    "Construction & Built Environment",
    "IT & Digital",
    "Health & Social Care",
  ],
};

const ME = {
  id: 1,
  email: "user@example.com",
  plan: "free",
  plan_limit: null,
  plan_used: 0,
  is_plan_active: false,
  has_unlimited: false,
};

function jsonResponse(data: unknown, ok = true, status = 200): Response {
  return { ok, status, json: async () => data } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;
let meResponse: { ok: boolean; status: number; body: unknown };
let savedGet: { sectors: string[] };

beforeEach(() => {
  meResponse = { ok: true, status: 200, body: ME };
  savedGet = { sectors: ["IT & Digital"] };
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/me/sectors")) {
      if (init?.method === "PUT") {
        const sent = JSON.parse(String(init.body)) as { sectors: string[] };
        // Echo what was sent — the real backend canonicalises/dedupes.
        return jsonResponse({ sectors: sent.sectors });
      }
      return jsonResponse(savedGet);
    }
    if (u.includes("/tenders/sectors")) return jsonResponse(SECTORS);
    if (u.endsWith("/me")) return jsonResponse(meResponse.body, meResponse.ok, meResponse.status);
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderWithAuth() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <AuthProvider>
        <SetupPage />
      </AuthProvider>
    </SWRConfig>,
  );
}

function putCalls() {
  return fetchMock.mock.calls.filter(
    (c) => String(c[0]).includes("/me/sectors") && c[1]?.method === "PUT",
  );
}

describe("SetupPage", () => {
  it("loads the user's saved sectors as pre-selected toggles", async () => {
    renderWithAuth();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "IT & Digital", pressed: true }),
      ).toBeInTheDocument(),
    );
    // An unselected sector is not pressed.
    expect(
      screen.getByRole("button", {
        name: "Construction & Built Environment",
        pressed: false,
      }),
    ).toBeInTheDocument();
  });

  it("saves the edited selection via PUT /me/sectors", async () => {
    renderWithAuth();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "IT & Digital", pressed: true }),
      ).toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Construction & Built Environment" }),
    );
    fireEvent.click(screen.getByRole("button", { name: /save sectors/i }));

    await waitFor(() => expect(putCalls().length).toBe(1));
    const body = JSON.parse(String(putCalls()[0][1]?.body)) as {
      sectors: string[];
    };
    expect(body.sectors).toEqual([
      "IT & Digital",
      "Construction & Built Environment",
    ]);
    await waitFor(() => expect(screen.getByText("Saved.")).toBeInTheDocument());
  });

  it("asks anonymous users to sign in (no save controls)", async () => {
    meResponse = { ok: false, status: 401, body: null };
    renderWithAuth();

    await waitFor(() =>
      expect(screen.getByText(/Sign in to save your sectors/i)).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: /save sectors/i }),
    ).not.toBeInTheDocument();
  });
});
