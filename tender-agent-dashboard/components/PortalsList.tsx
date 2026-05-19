"use client";

import Link from "next/link";
import { Suspense, useMemo, useState } from "react";
import useSWR from "swr";

import PillKicker from "./PillKicker";
import PortalCard from "./PortalCard";
import StatBlock from "./StatBlock";
import {
  type AdapterStatus,
  type ListPortalsParams,
  type Portal,
  type Priority,
  fetcher,
  portalsPath,
} from "@/lib/api";

const STATUS_OPTIONS: Array<{ value: AdapterStatus | ""; label: string }> = [
  { value: "", label: "All" },
  { value: "not_started", label: "Not started" },
  { value: "stub", label: "Stub" },
  { value: "read_only", label: "Read-only" },
  { value: "full", label: "Full" },
  { value: "deprecated", label: "Deprecated" },
];

const PRIORITY_OPTIONS: Array<{ value: Priority | ""; label: string }> = [
  { value: "", label: "All" },
  { value: "critical", label: "Critical" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

const SORT_OPTIONS: Array<{ value: ListPortalsParams["sort"]; label: string }> = [
  { value: "", label: "Priority" },
  { value: "tender_count", label: "Tender count (desc)" },
  { value: "last_seen", label: "Last seen (desc)" },
  { value: "first_seen", label: "First seen (asc)" },
];

export default function PortalsList() {
  return (
    <Suspense fallback={null}>
      <PortalsListInner />
    </Suspense>
  );
}

function PortalsListInner() {
  const [statusFilter, setStatusFilter] = useState<AdapterStatus | "">("");
  const [priorityFilter, setPriorityFilter] = useState<Priority | "">("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<ListPortalsParams["sort"]>("");

  const params: ListPortalsParams = {
    adapter_status: statusFilter || undefined,
    priority: priorityFilter || undefined,
    search: search || undefined,
    sort: sort || undefined,
    limit: 100,
  };
  const path = portalsPath(params);

  const { data, error, isLoading } = useSWR<Portal[]>(path, fetcher, {
    revalidateOnFocus: true,
  });

  const stats = useMemo(() => {
    const total = data?.length ?? 0;
    const highPriority = (data ?? []).filter(
      (p) => p.priority === "critical" || p.priority === "high",
    ).length;
    const adaptersBuilt = (data ?? []).filter(
      (p) => p.adapter_status === "read_only" || p.adapter_status === "full",
    ).length;
    const awaitingClassification = (data ?? []).filter(
      (p) =>
        p.adapter_status === "not_started" && p.classification_data === null,
    ).length;
    return { total, highPriority, adaptersBuilt, awaitingClassification };
  }, [data]);

  return (
    <div className="space-y-12 py-14">
      <header className="space-y-6">
        <PillKicker>Portal registry · live discovery</PillKicker>
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
          Where UK tenders live.
        </h1>
        <p
          className="text-text-muted"
          style={{ fontSize: "17px", lineHeight: 1.6, maxWidth: "720px" }}
        >
          All procurement portals discovered from your matched tenders.
          Classified, prioritised, and ready to integrate.
        </p>
        <div className="flex flex-wrap items-center gap-4 pt-2">
          <Link href="/portals/blocklist" className="btn-primary">
            Manage blocklist <span className="arrow">→</span>
          </Link>
          {data && (
            <span className="text-text-dim" style={{ fontSize: "13px" }}>
              {data.length} portal{data.length === 1 ? "" : "s"} returned
            </span>
          )}
        </div>
      </header>

      {data && data.length > 0 && (
        <div className="flex flex-wrap gap-x-14 gap-y-6 border-y border-border py-8">
          <StatBlock label="Total portals" value={stats.total.toString()} unit="in scope" />
          <StatBlock
            label="High / critical"
            value={stats.highPriority.toString()}
            unit={stats.highPriority === 1 ? "portal" : "portals"}
            tone={stats.highPriority > 0 ? "mint" : "default"}
          />
          <StatBlock
            label="Adapters built"
            value={stats.adaptersBuilt.toString()}
            unit={stats.adaptersBuilt === 1 ? "portal" : "portals"}
          />
          <StatBlock
            label="Awaiting classification"
            value={stats.awaitingClassification.toString()}
            unit={stats.awaitingClassification === 1 ? "portal" : "portals"}
          />
        </div>
      )}

      {/* Filter bar */}
      <div className="space-y-4">
        <FilterRow
          label="Adapter status"
          options={STATUS_OPTIONS}
          value={statusFilter}
          onChange={setStatusFilter}
        />
        <FilterRow
          label="Priority"
          options={PRIORITY_OPTIONS}
          value={priorityFilter}
          onChange={setPriorityFilter}
        />
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-3">
            <span
              className="text-text-dim"
              style={{ fontSize: "11px", letterSpacing: "0.12em", textTransform: "uppercase" }}
            >
              Search
            </span>
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="portal name or domain"
              className="bg-bg-elevated border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60"
              style={{ minWidth: "260px" }}
            />
          </div>
          <div className="flex items-center gap-3">
            <span
              className="text-text-dim"
              style={{ fontSize: "11px", letterSpacing: "0.12em", textTransform: "uppercase" }}
            >
              Sort by
            </span>
            <select
              value={sort ?? ""}
              onChange={(e) =>
                setSort(e.target.value as ListPortalsParams["sort"])
              }
              className="bg-bg-elevated border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60"
            >
              {SORT_OPTIONS.map((o) => (
                <option key={o.value || "default"} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded-xl border border-danger/40 bg-danger/5 p-5 text-text"
          style={{ borderRadius: "18px", fontSize: "14px" }}
        >
          Failed to load portals.
        </div>
      ) : isLoading && !data ? (
        <div className="text-text-dim" style={{ fontSize: "14px" }}>
          Loading…
        </div>
      ) : data && data.length === 0 ? (
        <div
          className="rounded-2xl border border-border bg-bg-elevated p-14 text-center backdrop-blur-md"
          style={{ borderRadius: "24px" }}
        >
          <h2
            className="font-display text-text mb-3"
            style={{ fontSize: "26px", letterSpacing: "-0.02em" }}
          >
            No portals match these filters.
          </h2>
          <p className="text-text-muted" style={{ fontSize: "14px" }}>
            Clear filters, or wait for the next ingestion to surface new
            domains.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          {data?.map((p) => <PortalCard key={p.id} portal={p} />)}
        </div>
      )}
    </div>
  );
}

function FilterRow<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: Array<{ value: T | ""; label: string }>;
  value: T | "";
  onChange: (v: T | "") => void;
}) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <span
        className="text-text-dim shrink-0"
        style={{
          fontSize: "11px",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          minWidth: "120px",
        }}
      >
        {label}
      </span>
      <div className="flex flex-wrap gap-2">
        {options.map((o) => {
          const active = value === o.value;
          return (
            <button
              key={o.value || "all"}
              type="button"
              onClick={() => onChange(o.value)}
              className={[
                "inline-flex items-center gap-2 rounded-pill border px-3.5 py-2 text-[13px] font-normal transition-colors backdrop-blur-sm focus-visible:outline-mint",
                active
                  ? "border-mint/40 bg-mint/10 text-mint-pale"
                  : "border-border-strong bg-bg-elevated text-text hover:border-mint-pale/40",
              ].join(" ")}
            >
              {o.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
