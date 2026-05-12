"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";

import TenderCard from "./TenderCard";
import {
  fetcher,
  tendersPath,
  type ListTendersParams,
  type SourceCode,
  type Tender,
} from "@/lib/api";

const SOURCES: { code: SourceCode; label: string }[] = [
  { code: "FTS", label: "Find a Tender" },
  { code: "CF", label: "Contracts Finder" },
  { code: "PCS", label: "Public Contracts Scotland" },
  { code: "S2W", label: "Sell2Wales" },
  { code: "NI", label: "eTendersNI" },
];

const PAGE_SIZE = 20;

function useDebouncedValue<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

export default function TenderList() {
  const [selectedSources, setSelectedSources] = useState<Set<SourceCode>>(new Set());
  const [buyer, setBuyer] = useState("");
  const [matchedOnly, setMatchedOnly] = useState(false);
  const [status, setStatus] = useState<"" | "active" | "closed">("");
  const [page, setPage] = useState(0);

  const debouncedBuyer = useDebouncedValue(buyer.trim(), 300);

  useEffect(() => {
    setPage(0);
  }, [selectedSources, debouncedBuyer, matchedOnly, status]);

  // Backend `source=` filter accepts a single code. If exactly one is selected we push
  // it server-side; with >1 we fetch the broader page and narrow client-side.
  const params: ListTendersParams = {
    buyer: debouncedBuyer || undefined,
    matched_only: matchedOnly || undefined,
    status: status || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    source: selectedSources.size === 1 ? Array.from(selectedSources)[0] : undefined,
  };

  const { data, error, isLoading, mutate } = useSWR<Tender[]>(
    tendersPath(params),
    fetcher,
    { revalidateOnFocus: true, keepPreviousData: true },
  );

  const visible = useMemo<Tender[]>(() => {
    if (!data) return [];
    if (selectedSources.size <= 1) return data;
    return data.filter((t) => selectedSources.has(t.source_code as SourceCode));
  }, [data, selectedSources]);

  const toggleSource = (code: SourceCode) => {
    setSelectedSources((cur) => {
      const next = new Set(cur);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  };

  return (
    <div className="space-y-6">
      <FilterBar
        selectedSources={selectedSources}
        toggleSource={toggleSource}
        buyer={buyer}
        setBuyer={setBuyer}
        matchedOnly={matchedOnly}
        setMatchedOnly={setMatchedOnly}
        status={status}
        setStatus={setStatus}
      />

      {error ? (
        <ErrorState onRetry={() => mutate()} />
      ) : isLoading && !data ? (
        <SkeletonList />
      ) : visible.length === 0 ? (
        <EmptyState matchedOnly={matchedOnly} />
      ) : (
        <ul className="space-y-3" aria-busy={isLoading}>
          {visible.map((t) => (
            <li key={t.id}>
              <TenderCard tender={t} />
            </li>
          ))}
        </ul>
      )}

      {!error && data && visible.length > 0 && (
        <Pagination
          page={page}
          hasNext={data.length === PAGE_SIZE}
          isLoading={isLoading}
          onPrev={() => setPage((p) => Math.max(0, p - 1))}
          onNext={() => setPage((p) => p + 1)}
        />
      )}
    </div>
  );
}

function FilterBar(props: {
  selectedSources: Set<SourceCode>;
  toggleSource: (code: SourceCode) => void;
  buyer: string;
  setBuyer: (v: string) => void;
  matchedOnly: boolean;
  setMatchedOnly: (v: boolean) => void;
  status: "" | "active" | "closed";
  setStatus: (v: "" | "active" | "closed") => void;
}) {
  return (
    <section
      aria-label="Filters"
      className="border border-bone/10 bg-ink-soft/40 p-4 space-y-3"
    >
      <div className="flex flex-wrap gap-2 items-center">
        <span
          className="text-[10px] font-mono uppercase tracking-widest text-bone/40 mr-2"
          id="source-filter-label"
        >
          source
        </span>
        <div className="flex flex-wrap gap-2" role="group" aria-labelledby="source-filter-label">
          {SOURCES.map((s) => {
            const active = props.selectedSources.has(s.code);
            return (
              <button
                key={s.code}
                type="button"
                aria-pressed={active}
                aria-label={`Filter by ${s.label}`}
                onClick={() => props.toggleSource(s.code)}
                className={`text-xs font-mono uppercase tracking-wider px-3 py-1.5 border transition focus:outline-none focus:ring-1 focus:ring-rust ${
                  active
                    ? "border-rust text-rust bg-rust/10"
                    : "border-bone/30 text-bone/70 hover:border-bone hover:text-bone"
                }`}
              >
                {s.code}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex flex-wrap gap-3 items-center">
        <label className="flex-1 min-w-[220px]">
          <span className="sr-only">Search buyer</span>
          <input
            type="search"
            value={props.buyer}
            onChange={(e) => props.setBuyer(e.target.value)}
            placeholder="search buyer…"
            aria-label="Search buyer"
            className="w-full bg-ink border border-bone/20 px-3 py-2 text-sm font-mono text-bone placeholder:text-bone/30 focus:outline-none focus:border-rust"
          />
        </label>

        <label className="flex items-center gap-2">
          <span className="sr-only">Status</span>
          <select
            value={props.status}
            onChange={(e) => props.setStatus(e.target.value as "" | "active" | "closed")}
            aria-label="Status"
            className="bg-ink border border-bone/20 px-3 py-2 text-xs font-mono uppercase tracking-wider text-bone focus:outline-none focus:border-rust"
          >
            <option value="">all status</option>
            <option value="active">active</option>
            <option value="closed">closed</option>
          </select>
        </label>

        <label className="flex items-center gap-2 text-xs font-mono uppercase tracking-wider text-bone/70 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={props.matchedOnly}
            onChange={(e) => props.setMatchedOnly(e.target.checked)}
            className="accent-rust h-4 w-4"
          />
          matched only
        </label>
      </div>
    </section>
  );
}

function SkeletonList() {
  return (
    <ul className="space-y-3" aria-label="Loading tenders" aria-busy="true">
      {Array.from({ length: 6 }).map((_, i) => (
        <li
          key={i}
          className="border border-bone/10 bg-ink-soft/30 p-5 animate-pulse"
        >
          <div className="flex items-center justify-between mb-3">
            <div className="h-3 w-24 bg-bone/10" />
            <div className="h-3 w-16 bg-bone/10" />
          </div>
          <div className="h-5 w-3/4 bg-bone/10 mb-2" />
          <div className="h-3 w-1/2 bg-bone/10 mb-4" />
          <div className="h-3 w-full bg-bone/5 mb-2" />
          <div className="flex items-end justify-between mt-4 pt-4 border-t border-bone/10">
            <div className="h-4 w-20 bg-bone/10" />
            <div className="h-3 w-16 bg-bone/5" />
          </div>
        </li>
      ))}
    </ul>
  );
}

function EmptyState({ matchedOnly }: { matchedOnly: boolean }) {
  return (
    <div className="border border-bone/10 bg-ink-soft/30 p-8 text-center space-y-3">
      <h2 className="font-display text-xl text-bone">
        {matchedOnly ? "No matches yet" : "No tenders to show"}
      </h2>
      <p className="text-sm text-bone/60 max-w-md mx-auto">
        {matchedOnly
          ? "Define a filter profile and wait for the next poll cycle to surface tenders that match your criteria."
          : "Adjust your filters above, or set up a filter profile to track new tenders that match your criteria."}
      </p>
      <div className="pt-2">
        <Link
          href="/filters"
          className="inline-block text-xs font-mono uppercase tracking-wider px-4 py-2 border border-rust text-rust hover:bg-rust hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
        >
          → manage filter profiles
        </Link>
      </div>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="border border-oxblood/60 bg-oxblood/10 p-6 space-y-3"
    >
      <h2 className="font-display text-lg text-bone">Couldn't load tenders</h2>
      <p className="text-sm text-bone/70">
        The backend at <span className="font-mono text-bone/90">/tenders</span> didn't
        respond. Check that the API is running, then retry.
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="text-xs font-mono uppercase tracking-wider px-4 py-2 border border-bone/30 hover:border-bone text-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
      >
        ↻ retry
      </button>
    </div>
  );
}

function Pagination({
  page,
  hasNext,
  isLoading,
  onPrev,
  onNext,
}: {
  page: number;
  hasNext: boolean;
  isLoading: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <nav
      aria-label="Pagination"
      className="flex items-center justify-between pt-2"
    >
      <button
        type="button"
        onClick={onPrev}
        disabled={page === 0 || isLoading}
        className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 border border-bone/30 text-bone/70 hover:border-bone hover:text-bone transition disabled:opacity-30 disabled:cursor-not-allowed focus:outline-none focus:ring-1 focus:ring-rust"
      >
        ← prev
      </button>
      <span
        className="text-xs font-mono uppercase tracking-wider text-bone/50"
        aria-live="polite"
      >
        page {page + 1}
      </span>
      <button
        type="button"
        onClick={onNext}
        disabled={!hasNext || isLoading}
        className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 border border-bone/30 text-bone/70 hover:border-bone hover:text-bone transition disabled:opacity-30 disabled:cursor-not-allowed focus:outline-none focus:ring-1 focus:ring-rust"
      >
        next →
      </button>
    </nav>
  );
}
