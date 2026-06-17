"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import useSWR from "swr";

import PillKicker from "./PillKicker";
import {
  fetcher,
  getMySectors,
  saveMySectors,
  type SectorTaxonomy,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

// Phase 3b — the setup/preferences page. The user picks their sectors of
// interest once and saves them; the selection persists PER-USER on the backend
// (account.saved_sectors) and pre-applies as the default sector filter on the
// dashboard. Reuses the existing session-cookie auth — anonymous users are
// asked to sign in first.

type SaveState = "idle" | "saving" | "saved" | "error";

export default function SetupPage() {
  const { me, loading: authLoading } = useAuth();

  // Canonical sectors — single source of truth from the backend taxonomy, the
  // same list the dashboard filter uses (no second copy that can drift).
  const taxonomy = useSWR<SectorTaxonomy>("/tenders/sectors", fetcher, {
    revalidateOnFocus: false,
  });
  const options = taxonomy.data?.sectors ?? [];

  const [selected, setSelected] = useState<string[]>([]);
  const [loadedSaved, setLoadedSaved] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");

  // Load THIS user's saved sectors once they're known to be logged in.
  useEffect(() => {
    if (!me) return;
    let cancelled = false;
    void getMySectors().then((saved) => {
      if (cancelled) return;
      setSelected(saved?.sectors ?? []);
      setLoadedSaved(true);
    });
    return () => {
      cancelled = true;
    };
  }, [me]);

  const toggle = (sector: string) => {
    setSaveState("idle");
    setSelected((s) =>
      s.includes(sector) ? s.filter((x) => x !== sector) : [...s, sector],
    );
  };

  const clearAll = () => {
    setSaveState("idle");
    setSelected([]);
  };

  const onSave = async () => {
    setSaveState("saving");
    try {
      const saved = await saveMySectors(selected);
      // The backend canonicalises/dedupes — reflect exactly what it stored.
      setSelected(saved.sectors);
      setSaveState("saved");
    } catch {
      setSaveState("error");
    }
  };

  return (
    <div className="space-y-10 py-14">
      <header className="space-y-6">
        <PillKicker withDot={false}>Setup</PillKicker>
        <h1
          className="font-display text-text text-balance"
          style={{
            fontSize: "clamp(40px, 5.5vw, 64px)",
            fontWeight: 400,
            letterSpacing: "-0.03em",
            lineHeight: 1.04,
            fontVariationSettings: '"opsz" 96',
            maxWidth: "900px",
          }}
        >
          Your sectors of interest.
        </h1>
        <p
          className="text-text-muted"
          style={{ fontSize: "17px", lineHeight: 1.6, maxWidth: "720px" }}
        >
          Pick the sectors you care about. We&apos;ll pre-apply them as the
          default filter every time you open the dashboard — you can still
          widen or narrow ad-hoc there without changing what&apos;s saved here.
        </p>
      </header>

      {authLoading ? (
        <PanelMessage>Loading…</PanelMessage>
      ) : !me ? (
        <PanelMessage>
          <span className="text-text">Sign in to save your sectors.</span>{" "}
          Your selection is stored against your account, so it&apos;s private
          to you and follows you across devices. Use the{" "}
          <span className="text-mint-pale">Sign in</span> button in the header,
          then return here.
        </PanelMessage>
      ) : (
        <section
          className="rounded-2xl border border-border-strong p-8 backdrop-blur-md space-y-8"
          style={{ background: "rgba(17, 23, 32, 0.82)", borderRadius: "24px" }}
          aria-label="Sector preferences"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span
              className="text-mint-pale"
              style={{
                fontSize: "11px",
                fontWeight: 500,
                letterSpacing: "0.14em",
                textTransform: "uppercase",
              }}
            >
              Sectors
            </span>
            <span className="text-[13px] text-text-dim">
              {selected.length === 0
                ? "None selected — the dashboard shows every sector"
                : `${selected.length} selected`}
            </span>
          </div>

          {taxonomy.isLoading || !loadedSaved ? (
            <span className="text-[13px] text-text-dim">Loading sectors…</span>
          ) : (
            <div className="flex flex-wrap gap-2.5">
              {options.map((sector) => {
                const active = selected.includes(sector);
                return (
                  <button
                    key={sector}
                    type="button"
                    aria-pressed={active}
                    onClick={() => toggle(sector)}
                    className={`rounded-full border px-3.5 py-1.5 text-[13px] transition-colors ${
                      active
                        ? "border-mint/60 bg-mint/15 text-text"
                        : "border-border-strong bg-bg-elevated text-text-muted hover:text-text"
                    }`}
                  >
                    {sector}
                  </button>
                );
              })}
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-4 border-t border-border pt-6">
            <div className="flex items-center gap-4 text-[13px]">
              {selected.length > 0 && (
                <button
                  type="button"
                  onClick={clearAll}
                  className="text-text-dim underline-offset-4 hover:text-text hover:underline"
                >
                  Clear all
                </button>
              )}
              {saveState === "saved" && (
                <span className="text-mint-pale" role="status">
                  Saved.
                </span>
              )}
              {saveState === "error" && (
                <span className="text-danger" role="alert">
                  Couldn&apos;t save — try again.
                </span>
              )}
            </div>
            <div className="flex flex-wrap gap-3">
              <Link href="/search" className="btn-secondary">
                Go to dashboard
              </Link>
              <button
                type="button"
                onClick={onSave}
                disabled={saveState === "saving" || !loadedSaved}
                className="btn-primary"
              >
                {saveState === "saving" ? "Saving…" : "Save sectors"}
              </button>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}

function PanelMessage({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-12 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <p className="text-text-muted" style={{ fontSize: "15px", lineHeight: 1.6 }}>
        {children}
      </p>
    </div>
  );
}
