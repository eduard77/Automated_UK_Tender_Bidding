"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import useSWR from "swr";

import FilterChip from "./FilterChip";
import PillKicker from "./PillKicker";
import StatBlock from "./StatBlock";
import TenderCard from "./TenderCard";
import {
  fetcher,
  tendersPath,
  type FilterProfile,
  type SourceCode,
  type Tender,
} from "@/lib/api";
import {
  absoluteDeadline,
  daysUntil,
  formatCurrency,
  formatCurrencyCompact,
  relativeAge,
  relativeDeadline,
  sourceLabel,
} from "@/lib/format";

type SortKey = "deadline-asc" | "value-desc" | "published-desc";

const PAGE_SIZE = 200;

export default function TenderList() {
  const [sort, setSort] = useState<SortKey>("deadline-asc");

  const tenders = useSWR<Tender[]>(
    tendersPath({ matched_only: true, limit: PAGE_SIZE }),
    fetcher,
    { revalidateOnFocus: true },
  );

  const filters = useSWR<FilterProfile[]>("/filters", fetcher, {
    revalidateOnFocus: false,
  });

  const activeFilters = useMemo<FilterProfile[]>(
    () => (filters.data ?? []).filter((f) => f.enabled),
    [filters.data],
  );

  const sorted = useMemo<Tender[]>(() => {
    const list = [...(tenders.data ?? [])];
    list.sort((a, b) => sortCompare(a, b, sort));
    return list;
  }, [tenders.data, sort]);

  // Featured = highest-value matched tender, regardless of current sort.
  // Falls back to first sorted item if no tender has a value.
  const featured = useMemo<Tender | undefined>(() => {
    if (!tenders.data?.length) return undefined;
    let best: Tender | undefined;
    let bestVal = -Infinity;
    for (const t of tenders.data) {
      const v = safeNumber(t.value_amount);
      if (v > bestVal) {
        best = t;
        bestVal = v;
      }
    }
    return best ?? sorted[0];
  }, [tenders.data, sorted]);
  const rest = featured ? sorted.filter((t) => t.id !== featured.id) : sorted;

  const stats = useMemo(() => computeStats(sorted, activeFilters), [sorted, activeFilters]);

  return (
    <>
      <section className="border-b border-border py-14 md:py-[88px]">
        <PillKicker>
          {tenders.isLoading
            ? "Construction desk · loading…"
            : `Construction desk · live from ${stats.sourcesActive} of 5 sources`}
        </PillKicker>

        <h1
          className="font-display text-text mt-7 text-balance"
          style={{
            fontSize: "clamp(48px, 6vw, 76px)",
            fontWeight: 400,
            letterSpacing: "-0.035em",
            lineHeight: 1.02,
            maxWidth: "920px",
            fontVariationSettings: '"opsz" 120',
          }}
        >
          Today&apos;s UK construction{" "}
          <span
            className="text-mint italic"
            style={{ fontWeight: 300 }}
          >
            signal.
          </span>
        </h1>

        <p
          className="mt-7 text-text-muted"
          style={{
            fontSize: "18px",
            maxWidth: "720px",
            lineHeight: 1.55,
          }}
        >
          {summarySentence(stats, activeFilters)}
        </p>

        <div className="mt-12 flex flex-wrap gap-x-14 gap-y-6">
          <StatBlock
            label="Matched today"
            value={tenders.isLoading ? "—" : stats.matched.toString()}
            unit={tenders.isLoading ? undefined : "tenders"}
          />
          <StatBlock
            label="Pipeline value"
            value={tenders.isLoading ? "—" : (stats.pipelineDisplay.value || "—")}
            unit={tenders.isLoading ? undefined : stats.pipelineDisplay.unit}
          />
          <StatBlock
            label="Sources active"
            value={tenders.isLoading ? "—" : stats.sourcesActive.toString()}
            unit={tenders.isLoading ? undefined : "of 5"}
          />
          <StatBlock
            label="Last poll"
            value={tenders.isLoading ? "—" : stats.lastPollDisplay.value}
            unit={tenders.isLoading ? undefined : stats.lastPollDisplay.unit}
          />
        </div>
      </section>

      <section className="flex flex-wrap items-center gap-3 border-b border-border py-6">
        {filters.isLoading ? (
          <span className="text-sm text-text-dim">Loading filters…</span>
        ) : activeFilters.length === 0 ? (
          <Link
            href="/filters"
            className="text-sm text-text-muted hover:text-text"
          >
            No filter profiles yet —{" "}
            <span className="text-mint-pale underline-offset-2 hover:underline">
              create one
            </span>
          </Link>
        ) : (
          activeFilters.map((f) => (
            <FilterChip key={f.id} label="Profile" value={f.name} variant="active" />
          ))
        )}

        {activeFilters[0]?.cpv_prefixes && activeFilters[0].cpv_prefixes.length > 0 && (
          <FilterChip
            label="CPV"
            value={`${activeFilters[0].cpv_prefixes.join(", ")} — broad`}
          />
        )}
        {activeFilters[0]?.min_days_to_deadline != null && (
          <FilterChip
            label="Deadline"
            value={`≥ ${activeFilters[0].min_days_to_deadline} days`}
          />
        )}

        <Link href="/filters" className="contents">
          <FilterChip variant="add" value="+ Manage filters" />
        </Link>

        <div className="flex-1" />

        <label className="flex items-center gap-2 text-[13px] text-text-muted">
          <span>Sort</span>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortKey)}
            className="cursor-pointer rounded-md border border-border-strong bg-bg-elevated px-3 py-2 text-[13px] text-text focus:outline-none"
          >
            <option value="deadline-asc">Deadline (soonest)</option>
            <option value="value-desc">Value (highest)</option>
            <option value="published-desc">Published (newest)</option>
          </select>
        </label>
      </section>

      {tenders.error ? (
        <ErrorState onRetry={() => tenders.mutate()} />
      ) : tenders.isLoading && !tenders.data ? (
        <ListSkeleton />
      ) : sorted.length === 0 ? (
        <EmptyState />
      ) : (
        <>
          {featured && (
            <section className="py-14">
              <div className="section-eyebrow mb-6">Featured opportunity</div>
              <FeaturedCard tender={featured} />
            </section>
          )}

          <section className="py-14 pb-24">
            <div className="section-eyebrow mb-6">
              All matched opportunities · {sorted.length}
            </div>
            <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
              {rest.map((t) => (
                <TenderCard key={t.id} tender={t} />
              ))}
            </div>
          </section>
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Featured card — inline because it has too many slots to share TenderCard.
// ---------------------------------------------------------------------------

function FeaturedCard({ tender }: { tender: Tender }) {
  const days = daysUntil(tender.deadline_at);
  const value = formatCurrency(tender.value_amount, tender.value_currency);
  const excerpt = tender.description
    ? tender.description.length > 360
      ? `${tender.description.slice(0, 360).trim()}…`
      : tender.description
    : "No description available from the source feed.";

  return (
    <article
      className="featured-glow-line relative overflow-hidden rounded-2xl border border-border-strong backdrop-blur-lg transition-all duration-200 hover:border-mint-pale/25"
      style={{
        background: "rgba(17, 23, 32, 0.82)",
        borderRadius: "24px",
        padding: "clamp(28px, 5vw, 48px) clamp(28px, 5vw, 56px) clamp(28px, 4vw, 44px)",
      }}
    >
      <div className="mb-9 flex flex-wrap items-start justify-between gap-6">
        <div className="flex flex-wrap items-center gap-3">
          <span
            className="font-mono text-text"
            style={{
              fontSize: "11px",
              fontWeight: 500,
              letterSpacing: "0.08em",
              padding: "4px 9px",
              background: "rgba(255, 255, 255, 0.06)",
              border: "1px solid rgba(255, 255, 255, 0.16)",
              borderRadius: "5px",
            }}
          >
            {tender.source_code}
          </span>
          {tender.notice_type && (
            <span
              className="text-text-muted"
              style={{
                fontSize: "12px",
                letterSpacing: "0.08em",
                textTransform: "uppercase",
              }}
            >
              {tender.notice_type}
            </span>
          )}
        </div>
        {value && (
          <span
            className="text-mint-pale"
            style={{
              fontSize: "12px",
              fontWeight: 500,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
            }}
          >
            ★ {formatCurrencyCompact(tender.value_amount, tender.value_currency)} — largest in pipeline
          </span>
        )}
      </div>

      <h2
        className="font-display text-text mb-[22px] text-balance"
        style={{
          fontSize: "clamp(34px, 4.4vw, 56px)",
          fontWeight: 400,
          letterSpacing: "-0.025em",
          lineHeight: 1.08,
          fontVariationSettings: '"opsz" 96',
          maxWidth: "1000px",
        }}
      >
        {tender.title}
      </h2>

      {tender.buyer_name && (
        <div
          className="mb-9 flex flex-wrap items-center gap-2.5 text-text-muted"
          style={{ fontSize: "17px" }}
        >
          <span className="text-text" style={{ fontWeight: 500 }}>
            {tender.buyer_name}
          </span>
        </div>
      )}

      <p
        className="text-text-muted mb-11"
        style={{
          fontSize: "17px",
          lineHeight: 1.6,
          maxWidth: "880px",
          letterSpacing: "-0.005em",
        }}
      >
        {excerpt}
      </p>

      <div className="flex flex-wrap items-end justify-between gap-8 border-t border-border pt-8">
        <div className="flex flex-wrap gap-x-14 gap-y-8">
          {value && (
            <div className="flex flex-col gap-2">
              <span
                className="text-text-dim"
                style={{
                  fontSize: "11px",
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  fontWeight: 500,
                }}
              >
                Estimated value
              </span>
              <span
                className="display-num text-mint-pale"
                style={{ fontSize: "32px", lineHeight: 1 }}
              >
                {value}
              </span>
            </div>
          )}
          <div className="flex flex-col gap-2">
            <span
              className="text-text-dim"
              style={{
                fontSize: "11px",
                letterSpacing: "0.12em",
                textTransform: "uppercase",
                fontWeight: 500,
              }}
            >
              Closes
            </span>
            <span
              className="display-num text-text"
              style={{ fontSize: "32px", lineHeight: 1 }}
            >
              {days !== null && days >= 0 ? `${days} day${days === 1 ? "" : "s"}` : "TBC"}
            </span>
            {tender.deadline_at && (
              <span
                className="text-text-dim"
                style={{ fontSize: "13px", marginTop: "4px" }}
              >
                {absoluteDeadline(tender.deadline_at)}
              </span>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-3">
          {tender.source_url && (
            <a
              href={tender.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="btn-secondary"
            >
              Open original
            </a>
          )}
          <Link href={`/tenders/${tender.id}`} className="btn-primary">
            Read brief <span className="arrow">→</span>
          </Link>
        </div>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Stats helpers
// ---------------------------------------------------------------------------

interface Stats {
  matched: number;
  pipelineDisplay: { value: string; unit: string };
  sourcesActive: number;
  lastPollDisplay: { value: string; unit: string };
}

function computeStats(tenders: Tender[], filters: FilterProfile[]): Stats {
  let totalValue = 0;
  const sources = new Set<string>();
  let mostRecentSeen: string | null = null;

  for (const t of tenders) {
    if (t.value_amount) {
      const n = Number(t.value_amount);
      if (Number.isFinite(n)) totalValue += n;
    }
    sources.add(t.source_code);
    if (!mostRecentSeen || (t.last_seen_at && t.last_seen_at > mostRecentSeen)) {
      mostRecentSeen = t.last_seen_at ?? mostRecentSeen;
    }
  }

  // Pipeline display: ranges into bn/M/k naturally.
  let pipelineValue = "—";
  let pipelineUnit = "";
  if (totalValue > 0) {
    if (totalValue >= 1_000_000_000) {
      pipelineValue = `£${(totalValue / 1_000_000_000).toFixed(1)}`;
      pipelineUnit = "billion";
    } else if (totalValue >= 1_000_000) {
      pipelineValue = `£${(totalValue / 1_000_000).toFixed(1)}`;
      pipelineUnit = "million";
    } else if (totalValue >= 1000) {
      pipelineValue = `£${Math.round(totalValue / 1000)}k`;
    } else {
      pipelineValue = `£${totalValue}`;
    }
  }

  // Last poll display: relative age from the most recent last_seen_at.
  let lpValue = "—";
  let lpUnit = "";
  if (mostRecentSeen) {
    const age = relativeAge(mostRecentSeen) ?? "—";
    // Reshape "12 min ago" into { value: 12, unit: "min ago" }
    const m = age.match(/^(\d+)\s+(.+)$/);
    if (m) {
      lpValue = m[1];
      lpUnit = m[2];
    } else {
      lpValue = age;
    }
  }
  void filters; // reserved for future per-filter breakdown
  return {
    matched: tenders.length,
    pipelineDisplay: { value: pipelineValue, unit: pipelineUnit },
    sourcesActive: sources.size,
    lastPollDisplay: { value: lpValue, unit: lpUnit },
  };
}

function summarySentence(stats: Stats, filters: FilterProfile[]): string {
  const profileLabel = filters[0]?.name ?? "your bid profile";
  return `${stats.matched} opportunit${stats.matched === 1 ? "y" : "ies"} matched against ${profileLabel}. Updated minutes ago from Find a Tender, Contracts Finder, and Public Contracts Scotland. The ones that close first are at the top.`;
}

// ---------------------------------------------------------------------------
// Sort
// ---------------------------------------------------------------------------

function sortCompare(a: Tender, b: Tender, key: SortKey): number {
  if (key === "value-desc") {
    return safeNumber(b.value_amount) - safeNumber(a.value_amount);
  }
  if (key === "published-desc") {
    return Date.parse(b.published_at ?? "") - Date.parse(a.published_at ?? "");
  }
  // deadline-asc — tenders without a deadline go last
  const ad = relativeDeadline(a.deadline_at);
  const bd = relativeDeadline(b.deadline_at);
  if (!a.deadline_at && b.deadline_at) return 1;
  if (a.deadline_at && !b.deadline_at) return -1;
  if (ad.closed && !bd.closed) return 1;
  if (!ad.closed && bd.closed) return -1;
  return Date.parse(a.deadline_at ?? "") - Date.parse(b.deadline_at ?? "");
}

function safeNumber(v: string | number | null | undefined): number {
  if (v === null || v === undefined) return 0;
  const n = typeof v === "string" ? Number(v) : v;
  return Number.isFinite(n) ? n : 0;
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

function ListSkeleton() {
  return (
    <>
      <section className="py-14">
        <div className="section-eyebrow mb-6">Loading…</div>
        <div
          className="shimmer rounded-2xl"
          style={{ height: "320px", borderRadius: "24px" }}
          aria-busy="true"
          aria-label="Loading featured tender"
        />
      </section>
      <section className="py-14 pb-24">
        <div className="section-eyebrow mb-6">All matched opportunities</div>
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="shimmer rounded-xl"
              style={{ height: "260px", borderRadius: "18px" }}
              aria-busy="true"
            />
          ))}
        </div>
      </section>
    </>
  );
}

function EmptyState() {
  return (
    <section className="py-24">
      <div
        className="mx-auto max-w-xl rounded-2xl border border-border bg-bg-elevated p-12 text-center backdrop-blur-md"
        style={{ borderRadius: "24px" }}
      >
        <svg
          width="80"
          height="80"
          viewBox="0 0 100 100"
          className="mx-auto mb-6 text-text-dim"
          fill="none"
          stroke="currentColor"
          strokeWidth="0.8"
          aria-hidden="true"
        >
          <circle cx="50" cy="50" r="32" />
          <circle cx="50" cy="50" r="14" />
          <line x1="50" y1="10" x2="50" y2="22" />
          <line x1="50" y1="78" x2="50" y2="90" />
          <line x1="10" y1="50" x2="22" y2="50" />
          <line x1="78" y1="50" x2="90" y2="50" />
        </svg>
        <h2
          className="font-display text-text mb-4"
          style={{ fontSize: "28px", letterSpacing: "-0.02em" }}
        >
          No matched opportunities yet.
        </h2>
        <p className="text-text-muted mb-7" style={{ fontSize: "15px", lineHeight: 1.55 }}>
          Define a filter profile and wait for the next poll cycle to surface tenders that match your bid criteria.
        </p>
        <Link href="/filters" className="btn-primary">
          Create a filter <span className="arrow">→</span>
        </Link>
      </div>
    </section>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <section className="py-14">
      <div
        role="alert"
        className="rounded-2xl border border-danger/40 bg-bg-elevated p-10 backdrop-blur-md"
        style={{ borderRadius: "24px" }}
      >
        <h2 className="font-display text-text" style={{ fontSize: "24px" }}>
          Couldn&apos;t load matched tenders.
        </h2>
        <p className="text-text-muted mt-3" style={{ fontSize: "15px" }}>
          The backend at <code className="font-mono text-text">/tenders</code> didn&apos;t
          respond. Check that the API is running, then retry.
        </p>
        <button type="button" onClick={onRetry} className="btn-secondary mt-6">
          ↻ Retry
        </button>
      </div>
    </section>
  );
}

// Used by parent route — re-export the only sort that affects URL state too.
export { type SortKey };
