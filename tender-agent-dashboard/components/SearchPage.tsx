"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";

import FilterChip from "./FilterChip";
import PillKicker from "./PillKicker";
import {
  fetcher,
  searchPath,
  type SearchSort,
  type TenderFacets,
  type TenderSearchParams,
  type TenderSearchResponse,
  type TenderSearchResult,
} from "@/lib/api";
import {
  absoluteDeadline,
  formatCurrency,
  formatCurrencyCompact,
  formatDate,
  relativeDeadline,
  sourceLabel,
} from "@/lib/format";

const PAGE_SIZE = 25;

// Filter-panel draft. Kept separate from the *applied* query so nothing
// queries until Search is pressed (click-to-search, per product decision).
interface Draft {
  q: string;
  cpv: string;
  // Region is now a multi-select of canonical regions (chunk 8), not free text.
  region: string[];
  buyer: string;
  valueMin: string;
  valueMax: string;
  // Off by default: a value range includes null-value tenders unless this is on.
  valueStatedOnly: boolean;
  deadlineFrom: string;
  deadlineTo: string;
  openOnly: boolean;
  statuses: string[];
  sources: string[];
  includeDuplicates: boolean;
}

const EMPTY_DRAFT: Draft = {
  q: "",
  cpv: "",
  region: [],
  buyer: "",
  valueMin: "",
  valueMax: "",
  valueStatedOnly: false,
  deadlineFrom: "",
  deadlineTo: "",
  openOnly: false,
  statuses: [],
  sources: [],
  includeDuplicates: true,
};

function splitList(raw: string): string[] {
  return raw
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

function draftToParams(d: Draft): TenderSearchParams {
  const hasRange = Boolean(d.valueMin.trim() || d.valueMax.trim());
  return {
    q: d.q.trim() || undefined,
    cpv: splitList(d.cpv),
    region: d.region,
    buyer: d.buyer.trim() || undefined,
    value_min: d.valueMin.trim() || undefined,
    value_max: d.valueMax.trim() || undefined,
    // Only meaningful alongside a range; drop it otherwise to keep URLs clean.
    value_stated_only: hasRange && d.valueStatedOnly ? true : undefined,
    deadline_from: d.deadlineFrom ? `${d.deadlineFrom}T00:00:00` : undefined,
    deadline_to: d.deadlineTo ? `${d.deadlineTo}T23:59:59` : undefined,
    open_only: d.openOnly || undefined,
    status: d.statuses.length ? d.statuses : undefined,
    source: d.sources.length ? d.sources : undefined,
    include_duplicates: d.includeDuplicates,
  };
}

// Rebuild the editable draft from applied params — used when an active-filter
// chip is removed, so the panel stays in lock-step with what's narrowing
// results.
function paramsToDraft(p: TenderSearchParams): Draft {
  return {
    q: p.q ?? "",
    cpv: (p.cpv ?? []).join(", "),
    region: p.region ?? [],
    buyer: p.buyer ?? "",
    valueMin: p.value_min != null ? String(p.value_min) : "",
    valueMax: p.value_max != null ? String(p.value_max) : "",
    valueStatedOnly: p.value_stated_only ?? false,
    deadlineFrom: p.deadline_from ? p.deadline_from.slice(0, 10) : "",
    deadlineTo: p.deadline_to ? p.deadline_to.slice(0, 10) : "",
    openOnly: p.open_only ?? false,
    statuses: p.status ?? [],
    sources: p.source ?? [],
    includeDuplicates: p.include_duplicates ?? true,
  };
}

const STATUS_LABELS: Record<string, string> = {
  active: "Active",
  closed: "Closed",
  planned: "Planned",
  awarded: "Awarded",
  cancelled: "Cancelled",
};

function statusLabel(s: string): string {
  return STATUS_LABELS[s] ?? s.charAt(0).toUpperCase() + s.slice(1);
}

export default function SearchPage() {
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  // `applied` holds only the filter fields; sort + page live alongside so they
  // re-query an already-submitted search without needing another Search press.
  const [applied, setApplied] = useState<TenderSearchParams | null>(null);
  const [sort, setSort] = useState<SearchSort>("deadline_asc");
  const [page, setPage] = useState(1);

  const facets = useSWR<TenderFacets>("/tenders/facets", fetcher, {
    revalidateOnFocus: false,
  });

  const key = applied
    ? searchPath({ ...applied, sort, page, page_size: PAGE_SIZE })
    : null;
  const { data, error, isLoading } = useSWR<TenderSearchResponse>(key, fetcher, {
    revalidateOnFocus: false,
    keepPreviousData: true,
  });

  const set = <K extends keyof Draft>(field: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [field]: value }));

  const toggleIn = (
    field: "statuses" | "sources" | "region",
    value: string,
  ) =>
    setDraft((d) => {
      const has = d[field].includes(value);
      return {
        ...d,
        [field]: has
          ? d[field].filter((v) => v !== value)
          : [...d[field], value],
      };
    });

  const onSearch = (e: React.FormEvent) => {
    e.preventDefault();
    setPage(1);
    setApplied(draftToParams(draft));
  };

  const onClear = () => {
    setDraft(EMPTY_DRAFT);
    setApplied(null);
    setSort("deadline_asc");
    setPage(1);
  };

  // Remove one active filter: patch the *applied* params, re-sync the draft so
  // the panel matches, and re-query from page 1. Re-broadens correctly because
  // the dropped clause simply leaves the WHERE.
  const patchApplied = (patch: Partial<TenderSearchParams>) => {
    if (!applied) return;
    const next = { ...applied, ...patch };
    setApplied(next);
    setDraft(paramsToDraft(next));
    setPage(1);
  };

  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const hasSearched = applied !== null;

  return (
    <div className="space-y-10 py-14">
      <header className="space-y-6">
        <PillKicker withDot={false}>Search</PillKicker>
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
          Find any tender.
        </h1>
        <p
          className="text-text-muted"
          style={{ fontSize: "17px", lineHeight: 1.6, maxWidth: "720px" }}
        >
          Search every source in one place — Contracts Finder, Find a Tender,
          Public Contracts Scotland, and any portal added later. Set your filters,
          then press Search. Duplicate notices are shown and clearly marked.
        </p>
      </header>

      {/* Filter panel ----------------------------------------------------- */}
      <form
        onSubmit={onSearch}
        className="rounded-2xl border border-border-strong p-8 backdrop-blur-md space-y-8"
        style={{ background: "rgba(17, 23, 32, 0.82)", borderRadius: "24px" }}
        aria-label="Tender search filters"
      >
        <FieldGroup>
          <Field
            label="Search text"
            value={draft.q}
            onChange={(v) => set("q", v)}
            placeholder="e.g. school refurbishment, demolition, highways"
            help="Matches title, description and keywords"
          />
        </FieldGroup>

        <Fieldset legend="Classification & buyer">
          <FieldGroup cols={2}>
            <Field
              label="CPV codes"
              value={draft.cpv}
              onChange={(v) => set("cpv", v)}
              placeholder="45000000, 45210000"
              mono
              help="Comma-separated; matches any overlap"
            />
            <Field
              label="Buyer"
              value={draft.buyer}
              onChange={(v) => set("buyer", v)}
              placeholder="Bristol City Council"
            />
            <RegionDropdown
              options={facets.data?.regions ?? []}
              selected={draft.region}
              onToggle={(v) => toggleIn("region", v)}
              onClearAll={() => set("region", [])}
              loading={facets.isLoading}
            />
          </FieldGroup>
        </Fieldset>

        <Fieldset legend="Value & deadline">
          <FieldGroup cols={2}>
            <NumberField
              label="Value min"
              value={draft.valueMin}
              onChange={(v) => set("valueMin", v)}
              placeholder="50000"
              prefix="£"
            />
            <NumberField
              label="Value max"
              value={draft.valueMax}
              onChange={(v) => set("valueMax", v)}
              placeholder="500000"
              prefix="£"
            />
            <DateField
              label="Deadline from"
              value={draft.deadlineFrom}
              onChange={(v) => set("deadlineFrom", v)}
            />
            <DateField
              label="Deadline to"
              value={draft.deadlineTo}
              onChange={(v) => set("deadlineTo", v)}
            />
          </FieldGroup>
          <div className="mt-4 space-y-3">
            <label className="flex w-fit items-center gap-3 text-text-muted text-[13px]">
              <input
                type="checkbox"
                checked={draft.valueStatedOnly}
                onChange={(e) => set("valueStatedOnly", e.target.checked)}
                className="h-4 w-4 cursor-pointer accent-mint"
              />
              Only tenders with a stated value
              <span className="text-text-dim">
                (off: a value range still includes notices with no stated value)
              </span>
            </label>
            <label className="flex w-fit items-center gap-3 text-text-muted text-[13px]">
              <input
                type="checkbox"
                checked={draft.openOnly}
                onChange={(e) => set("openOnly", e.target.checked)}
                className="h-4 w-4 cursor-pointer accent-mint"
              />
              Open only (live notices — future deadline, still active)
            </label>
          </div>
        </Fieldset>

        <Fieldset legend="Status">
          {facets.isLoading ? (
            <span className="text-[13px] text-text-dim">Loading…</span>
          ) : (
            <ChipChoices
              options={facets.data?.statuses ?? []}
              selected={draft.statuses}
              onToggle={(v) => toggleIn("statuses", v)}
              labeller={statusLabel}
            />
          )}
        </Fieldset>

        <Fieldset legend="Source (default: all)">
          {facets.isLoading ? (
            <span className="text-[13px] text-text-dim">Loading…</span>
          ) : (
            <ChipChoices
              options={facets.data?.sources ?? []}
              selected={draft.sources}
              onToggle={(v) => toggleIn("sources", v)}
              labeller={sourceLabel}
            />
          )}
        </Fieldset>

        <div className="flex flex-wrap items-center justify-between gap-4 border-t border-border pt-6">
          <label className="flex items-center gap-3 text-text-muted text-[13px]">
            <input
              type="checkbox"
              checked={draft.includeDuplicates}
              onChange={(e) => set("includeDuplicates", e.target.checked)}
              className="h-4 w-4 cursor-pointer accent-mint"
            />
            Include duplicates (shown and marked)
          </label>
          <div className="flex flex-wrap gap-3">
            <button type="button" onClick={onClear} className="btn-secondary">
              Clear
            </button>
            <button type="submit" className="btn-primary">
              Search <span className="arrow">→</span>
            </button>
          </div>
        </div>
      </form>

      {/* Active-filter chips — exactly what's narrowing the results, removable. */}
      {hasSearched && applied && (
        <ActiveFilterChips
          applied={applied}
          onPatch={patchApplied}
          onClearAll={onClear}
        />
      )}

      {/* Results ---------------------------------------------------------- */}
      {!hasSearched ? (
        <PromptState />
      ) : error ? (
        <ErrorState />
      ) : (
        <section className="space-y-6">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="section-eyebrow flex-1">
              {isLoading && !data
                ? "Searching…"
                : `${total.toLocaleString("en-GB")} tender${total === 1 ? "" : "s"} match`}
            </div>
            <label className="flex items-center gap-2 text-[13px] text-text-muted">
              <span>Sort</span>
              <select
                value={sort}
                onChange={(e) => {
                  setSort(e.target.value as SearchSort);
                  setPage(1);
                }}
                className="cursor-pointer rounded-md border border-border-strong bg-bg-elevated px-3 py-2 text-[13px] text-text focus:outline-none"
              >
                <option value="deadline_asc">Closing soonest</option>
                <option value="published_desc">Newest</option>
                <option value="value_desc">Highest value</option>
              </select>
            </label>
          </div>

          {isLoading && !data ? (
            <ResultsSkeleton />
          ) : data && data.results.length === 0 ? (
            <EmptyState />
          ) : (
            <>
              <ul className="space-y-4">
                {data?.results.map((r) => (
                  <li key={r.id}>
                    <ResultCard result={r} />
                  </li>
                ))}
              </ul>
              <Pagination
                page={page}
                totalPages={totalPages}
                onPrev={() => setPage((p) => Math.max(1, p - 1))}
                onNext={() => setPage((p) => Math.min(totalPages, p + 1))}
              />
            </>
          )}
        </section>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Result card
// ---------------------------------------------------------------------------

function resultValue(r: TenderSearchResult): string | null {
  const amount = formatCurrency(r.value_amount, r.value_currency);
  if (amount) return amount;
  const lo = formatCurrencyCompact(r.value_min, r.value_currency);
  const hi = formatCurrencyCompact(r.value_max, r.value_currency);
  if (lo && hi) return `${lo}–${hi}`;
  if (lo) return `from ${lo}`;
  if (hi) return `up to ${hi}`;
  return null;
}

function ResultCard({ result: r }: { result: TenderSearchResult }) {
  const deadline = relativeDeadline(r.deadline_at);
  const value = resultValue(r);
  // Prefer the raw source locality; fall back to the canonical region (chunk 8).
  const location = r.buyer_region || r.region;

  return (
    <article
      className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md transition-colors hover:border-mint-pale/30"
      style={{ borderRadius: "18px" }}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2.5">
        <span
          className="font-mono text-text-muted"
          style={{
            fontSize: "11px",
            fontWeight: 500,
            letterSpacing: "0.08em",
            padding: "3px 8px",
            background: "rgba(255, 255, 255, 0.04)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "4px",
          }}
          title={r.source_code}
        >
          {sourceLabel(r.source_code)}
        </span>
        {r.status && (
          <span
            className="text-text-dim"
            style={{
              fontSize: "11px",
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              fontWeight: 500,
            }}
          >
            {statusLabel(r.status)}
          </span>
        )}
        {r.is_duplicate && (
          <Link
            href={r.duplicate_of_id ? `/tenders/${r.duplicate_of_id}` : "#"}
            className="inline-flex items-center gap-1.5 text-amber"
            style={{
              fontSize: "11px",
              fontWeight: 500,
              letterSpacing: "0.04em",
              padding: "2px 8px",
              background: "rgba(245, 166, 35, 0.1)",
              border: "1px solid rgba(245, 166, 35, 0.3)",
              borderRadius: "100px",
            }}
            title={
              r.duplicate_of_id
                ? `Duplicate of tender #${r.duplicate_of_id} — open the primary listing`
                : "Duplicate notice"
            }
          >
            ⧉ Duplicate
          </Link>
        )}
        {!deadline.closed && deadline.urgent && (
          <span
            className="ml-auto inline-flex items-center gap-1.5 text-amber"
            style={{
              fontSize: "11px",
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              fontWeight: 500,
            }}
          >
            <span className="inline-block h-[5px] w-[5px] rounded-full bg-amber" />
            {deadline.text}
          </span>
        )}
      </div>

      <h2
        className="font-display text-text mb-2"
        style={{
          fontSize: "20px",
          fontWeight: 400,
          letterSpacing: "-0.018em",
          lineHeight: 1.2,
        }}
      >
        <Link
          href={`/tenders/${r.id}`}
          className="outline-none transition-colors hover:text-mint-pale focus-visible:text-mint-pale"
        >
          {r.title}
        </Link>
      </h2>

      <p className="mb-4 text-[14px] leading-[1.4] text-text-muted">
        {r.buyer_name && (
          <span className="text-text" style={{ fontWeight: 450 }}>
            {r.buyer_name}
          </span>
        )}
        {r.buyer_name && location && <span> · </span>}
        {location && <span>{location}</span>}
        {!r.buyer_name && !location && (
          <span className="italic text-text-dim">Buyer not stated</span>
        )}
      </p>

      {r.cpv_codes && r.cpv_codes.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-1.5">
          {r.cpv_codes.slice(0, 4).map((code) => (
            <span
              key={code}
              className="font-mono text-text-muted"
              style={{
                fontSize: "10px",
                padding: "3px 7px",
                background: "rgba(255, 255, 255, 0.04)",
                borderRadius: "3px",
                letterSpacing: "0.02em",
              }}
            >
              {code}
            </span>
          ))}
          {r.cpv_codes.length > 4 && (
            <span className="font-mono text-text-dim" style={{ fontSize: "10px", padding: "3px 7px" }}>
              +{r.cpv_codes.length - 4}
            </span>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-4 border-t border-border pt-4">
        <span
          className={`display-num ${value ? "text-mint-pale" : "italic text-text-dim"}`}
          style={{ fontSize: "17px", lineHeight: 1 }}
        >
          {value ?? "Value not stated"}
        </span>
        <span
          className={`text-[13px] ${deadline.urgent && !deadline.closed ? "text-amber" : "text-text-muted"}`}
        >
          {r.deadline_at
            ? `${deadline.closed ? "Closed" : "Closes"} ${absoluteDeadline(r.deadline_at) ?? formatDate(r.deadline_at)}`
            : "Deadline TBC"}
        </span>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

function Pagination({
  page,
  totalPages,
  onPrev,
  onNext,
}: {
  page: number;
  totalPages: number;
  onPrev: () => void;
  onNext: () => void;
}) {
  if (totalPages <= 1) return null;
  return (
    <nav
      className="flex items-center justify-center gap-4 pt-2"
      aria-label="Search results pages"
    >
      <button
        type="button"
        onClick={onPrev}
        disabled={page <= 1}
        className="btn-ghost"
      >
        ← Prev
      </button>
      <span className="text-[13px] text-text-muted">
        Page <span className="text-text">{page}</span> of {totalPages}
      </span>
      <button
        type="button"
        onClick={onNext}
        disabled={page >= totalPages}
        className="btn-ghost"
      >
        Next →
      </button>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Active-filter chips
// ---------------------------------------------------------------------------

function ActiveFilterChips({
  applied,
  onPatch,
  onClearAll,
}: {
  applied: TenderSearchParams;
  onPatch: (patch: Partial<TenderSearchParams>) => void;
  onClearAll: () => void;
}) {
  const a = applied;
  const chips: React.ReactNode[] = [];
  const money = (v: string | number) => `£${Number(v).toLocaleString("en-GB")}`;

  if (a.q)
    chips.push(
      <FilterChip key="q" label="Text" value={a.q} onRemove={() => onPatch({ q: undefined })} />,
    );
  for (const code of a.cpv ?? [])
    chips.push(
      <FilterChip
        key={`cpv:${code}`}
        label="CPV"
        value={code}
        onRemove={() => onPatch({ cpv: (a.cpv ?? []).filter((c) => c !== code) })}
      />,
    );
  for (const r of a.region ?? [])
    chips.push(
      <FilterChip
        key={`region:${r}`}
        label="Region"
        value={r}
        onRemove={() => onPatch({ region: (a.region ?? []).filter((x) => x !== r) })}
      />,
    );
  if (a.buyer)
    chips.push(
      <FilterChip
        key="buyer"
        label="Buyer"
        value={a.buyer}
        onRemove={() => onPatch({ buyer: undefined })}
      />,
    );
  if (a.value_min != null || a.value_max != null) {
    const lo = a.value_min != null ? money(a.value_min) : "";
    const hi = a.value_max != null ? money(a.value_max) : "";
    const text = lo && hi ? `${lo}–${hi}` : lo ? `from ${lo}` : `up to ${hi}`;
    chips.push(
      <FilterChip
        key="value"
        label="Value"
        value={text}
        onRemove={() =>
          onPatch({ value_min: undefined, value_max: undefined, value_stated_only: undefined })
        }
      />,
    );
  }
  if (a.value_stated_only)
    chips.push(
      <FilterChip
        key="stated"
        value="Stated value only"
        onRemove={() => onPatch({ value_stated_only: undefined })}
      />,
    );
  if (a.deadline_from || a.deadline_to) {
    const lo = a.deadline_from ? a.deadline_from.slice(0, 10) : "";
    const hi = a.deadline_to ? a.deadline_to.slice(0, 10) : "";
    const text = lo && hi ? `${lo} → ${hi}` : lo ? `from ${lo}` : `until ${hi}`;
    chips.push(
      <FilterChip
        key="deadline"
        label="Deadline"
        value={text}
        onRemove={() => onPatch({ deadline_from: undefined, deadline_to: undefined })}
      />,
    );
  }
  if (a.open_only)
    chips.push(
      <FilterChip key="open" value="Open only" onRemove={() => onPatch({ open_only: undefined })} />,
    );
  for (const s of a.status ?? [])
    chips.push(
      <FilterChip
        key={`status:${s}`}
        label="Status"
        value={statusLabel(s)}
        onRemove={() => onPatch({ status: (a.status ?? []).filter((x) => x !== s) })}
      />,
    );
  for (const s of a.source ?? [])
    chips.push(
      <FilterChip
        key={`source:${s}`}
        label="Source"
        value={sourceLabel(s)}
        onRemove={() => onPatch({ source: (a.source ?? []).filter((x) => x !== s) })}
      />,
    );
  if (a.include_duplicates === false)
    chips.push(
      <FilterChip
        key="dups"
        value="Excluding duplicates"
        onRemove={() => onPatch({ include_duplicates: true })}
      />,
    );

  if (chips.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2.5">
      <span className="section-eyebrow">Filters</span>
      {chips}
      <button
        type="button"
        onClick={onClearAll}
        className="text-[13px] text-text-dim underline-offset-4 hover:text-text hover:underline"
      >
        Reset all
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Region multi-select dropdown (canonical regions present, with counts)
// ---------------------------------------------------------------------------

function RegionDropdown({
  options,
  selected,
  onToggle,
  onClearAll,
  loading,
}: {
  options: { value: string; count: number }[];
  selected: string[];
  onToggle: (v: string) => void;
  onClearAll: () => void;
  loading: boolean;
}) {
  const summary =
    selected.length === 0
      ? "All regions"
      : selected.length === 1
        ? selected[0]
        : `${selected.length} regions`;
  return (
    <label className="block">
      <FieldLabel>Region</FieldLabel>
      <details className="group relative mt-2">
        <summary
          className="flex cursor-pointer list-none items-center justify-between rounded-md border border-border-strong bg-bg-elevated px-3.5 py-2.5 text-[14px] text-text transition-colors hover:border-mint-pale/40 focus-visible:border-mint focus-visible:outline-none [&::-webkit-details-marker]:hidden"
          aria-label="Select regions"
        >
          <span className={selected.length === 0 ? "text-text-dim" : "text-text"}>
            {summary}
          </span>
          <span className="text-text-dim transition-transform group-open:rotate-180" aria-hidden>
            ▾
          </span>
        </summary>
        <div
          className="absolute z-20 mt-2 max-h-64 w-full overflow-auto rounded-md border border-border-strong bg-bg-elevated p-1.5 shadow-xl"
          style={{ background: "rgba(17, 23, 32, 0.98)" }}
        >
          {loading ? (
            <span className="block px-2 py-2 text-[13px] text-text-dim">Loading…</span>
          ) : options.length === 0 ? (
            <span className="block px-2 py-2 text-[13px] text-text-dim">
              No regions yet.
            </span>
          ) : (
            <>
              {selected.length > 0 && (
                <button
                  type="button"
                  onClick={onClearAll}
                  className="mb-1 w-full rounded px-2 py-1.5 text-left text-[12px] text-text-dim hover:bg-white/5 hover:text-text"
                >
                  Clear region selection
                </button>
              )}
              {options.map((o) => (
                <label
                  key={o.value}
                  className="flex cursor-pointer items-center justify-between gap-3 rounded px-2 py-1.5 hover:bg-white/5"
                >
                  <span className="flex items-center gap-2.5 text-[13px] text-text">
                    <input
                      type="checkbox"
                      checked={selected.includes(o.value)}
                      onChange={() => onToggle(o.value)}
                      className="h-3.5 w-3.5 cursor-pointer accent-mint"
                    />
                    {o.value}
                  </span>
                  <span className="font-mono text-[11px] text-text-dim">
                    {o.count.toLocaleString("en-GB")}
                  </span>
                </label>
              ))}
            </>
          )}
        </div>
      </details>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Form primitives (mirror FiltersManager styling)
// ---------------------------------------------------------------------------

function Fieldset({ legend, children }: { legend: string; children: React.ReactNode }) {
  return (
    <fieldset className="space-y-4">
      <legend
        className="text-mint-pale"
        style={{
          fontSize: "11px",
          fontWeight: 500,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
          marginBottom: "12px",
        }}
      >
        {legend}
      </legend>
      {children}
    </fieldset>
  );
}

function FieldGroup({ cols = 1, children }: { cols?: 1 | 2; children: React.ReactNode }) {
  const grid =
    cols === 1
      ? "grid grid-cols-1 gap-5"
      : "grid grid-cols-1 gap-5 md:grid-cols-2";
  return <div className={grid}>{children}</div>;
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="block text-text-dim"
      style={{
        fontSize: "11px",
        fontWeight: 500,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
      }}
    >
      {children}
    </span>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  mono,
  help,
  listId,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  help?: string;
  listId?: string;
}) {
  return (
    <label className="block">
      <FieldLabel>{label}</FieldLabel>
      <input
        type="text"
        value={value}
        list={listId}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`mt-2 w-full rounded-md border border-border-strong bg-bg-elevated px-3.5 py-2.5 text-text placeholder:text-text-dim transition-colors focus:border-mint focus:outline-none ${mono ? "font-mono text-[13px]" : "text-[14px]"}`}
      />
      {help && (
        <span className="mt-2 block text-text-dim" style={{ fontSize: "11px", letterSpacing: "0.04em" }}>
          {help}
        </span>
      )}
    </label>
  );
}

function NumberField({
  label,
  value,
  onChange,
  placeholder,
  prefix,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  prefix?: string;
}) {
  return (
    <label className="block">
      <FieldLabel>{label}</FieldLabel>
      <div className="mt-2 relative">
        {prefix && (
          <span
            className="absolute inset-y-0 left-3.5 flex items-center text-text-muted"
            style={{ fontSize: "14px" }}
            aria-hidden="true"
          >
            {prefix}
          </span>
        )}
        <input
          type="number"
          inputMode="decimal"
          min="0"
          step="any"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className={`w-full rounded-md border border-border-strong bg-bg-elevated py-2.5 ${prefix ? "pl-9" : "pl-3.5"} pr-3.5 font-mono text-[13px] text-text placeholder:text-text-dim transition-colors focus:border-mint focus:outline-none`}
        />
      </div>
    </label>
  );
}

function DateField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="block">
      <FieldLabel>{label}</FieldLabel>
      <input
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-2 w-full rounded-md border border-border-strong bg-bg-elevated px-3.5 py-2.5 text-[14px] text-text transition-colors focus:border-mint focus:outline-none"
        style={{ colorScheme: "dark" }}
      />
    </label>
  );
}

function ChipChoices({
  options,
  selected,
  onToggle,
  labeller,
}: {
  options: string[];
  selected: string[];
  onToggle: (v: string) => void;
  labeller: (v: string) => string;
}) {
  if (options.length === 0) {
    return <span className="text-[13px] text-text-dim">None available.</span>;
  }
  return (
    <div className="flex flex-wrap gap-2.5">
      {options.map((opt) => {
        const active = selected.includes(opt);
        return (
          <button
            key={opt}
            type="button"
            aria-pressed={active}
            onClick={() => onToggle(opt)}
            className={`rounded-full border px-3.5 py-1.5 text-[13px] transition-colors ${
              active
                ? "border-mint/60 bg-mint/15 text-text"
                : "border-border-strong bg-bg-elevated text-text-muted hover:text-text"
            }`}
          >
            {labeller(opt)}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

function PromptState() {
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-12 text-center backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <p className="text-text-muted" style={{ fontSize: "15px", lineHeight: 1.55 }}>
        Set your filters above and press{" "}
        <span className="text-mint-pale">Search</span> to query every source. Leave
        everything blank to browse the full set, closing soonest first.
      </p>
    </div>
  );
}

function EmptyState() {
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-12 text-center backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <h2 className="font-display text-text mb-3" style={{ fontSize: "24px", letterSpacing: "-0.02em" }}>
        No tenders match these filters.
      </h2>
      <p className="text-text-muted" style={{ fontSize: "15px" }}>
        Try widening the value range, clearing the deadline window, or removing a
        source restriction.
      </p>
    </div>
  );
}

function ErrorState() {
  return (
    <div
      role="alert"
      className="rounded-2xl border border-danger/40 bg-bg-elevated p-10 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <h2 className="font-display text-text" style={{ fontSize: "24px" }}>
        Search failed.
      </h2>
      <p className="text-text-muted mt-3" style={{ fontSize: "15px" }}>
        The backend at <code className="font-mono text-text">/tenders/search</code>{" "}
        didn&apos;t respond. Check that the API is running, then search again.
      </p>
    </div>
  );
}

function ResultsSkeleton() {
  return (
    <ul className="space-y-4" aria-busy="true" aria-label="Searching">
      {Array.from({ length: 5 }).map((_, i) => (
        <li
          key={i}
          className="shimmer rounded-2xl"
          style={{ height: "180px", borderRadius: "18px" }}
        />
      ))}
    </ul>
  );
}
