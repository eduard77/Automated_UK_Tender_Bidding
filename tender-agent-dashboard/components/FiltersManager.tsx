"use client";

import { useState } from "react";
import useSWR from "swr";

import {
  ApiError,
  createFilter,
  deleteFilter,
  fetcher,
  replaceFilter,
  updateFilter,
  type FilterProfile,
  type FilterProfileCreate,
} from "@/lib/api";

const FILTERS_KEY = "/filters";

export default function FiltersManager() {
  const { data, error, isLoading, mutate } = useSWR<FilterProfile[]>(
    FILTERS_KEY,
    fetcher,
    { revalidateOnFocus: true },
  );

  const [isCreating, setIsCreating] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [topError, setTopError] = useState<string | null>(null);

  const handleCreate = async (payload: FilterProfileCreate) => {
    setTopError(null);
    try {
      const created = await createFilter(payload);
      await mutate((cur) => [created, ...(cur ?? [])], { revalidate: true });
      setIsCreating(false);
    } catch (err) {
      setTopError(formatError(err));
    }
  };

  const handleReplace = async (id: number, payload: FilterProfileCreate) => {
    setTopError(null);
    try {
      const updated = await replaceFilter(id, payload);
      await mutate(
        (cur) => (cur ?? []).map((f) => (f.id === id ? updated : f)),
        { revalidate: false },
      );
      setEditingId(null);
    } catch (err) {
      setTopError(formatError(err));
    }
  };

  const handleToggleEnabled = async (filter: FilterProfile) => {
    const next = !filter.enabled;
    // Optimistic update.
    await mutate(
      (cur) =>
        (cur ?? []).map((f) => (f.id === filter.id ? { ...f, enabled: next } : f)),
      { revalidate: false },
    );
    try {
      await updateFilter(filter.id, { enabled: next });
    } catch (err) {
      setTopError(formatError(err));
      await mutate(); // roll back via fresh fetch
    }
  };

  const handleDelete = async (filter: FilterProfile) => {
    const ok = window.confirm(
      `Delete filter "${filter.name}"? This will also delete its match history.`,
    );
    if (!ok) return;
    // Optimistic remove.
    await mutate(
      (cur) => (cur ?? []).filter((f) => f.id !== filter.id),
      { revalidate: false },
    );
    try {
      await deleteFilter(filter.id);
    } catch (err) {
      setTopError(formatError(err));
      await mutate(); // roll back
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="text-sm text-bone/60">
          {data ? `${data.length} filter${data.length === 1 ? "" : "s"}` : "—"}
        </div>
        {!isCreating && (
          <button
            type="button"
            onClick={() => {
              setIsCreating(true);
              setEditingId(null);
            }}
            className="text-xs font-mono uppercase tracking-wider px-4 py-2 border border-rust text-rust hover:bg-rust hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
          >
            + new filter profile
          </button>
        )}
      </div>

      {topError && (
        <div role="alert" className="border border-oxblood/60 bg-oxblood/10 p-3 text-sm text-bone">
          {topError}
        </div>
      )}

      {isCreating && (
        <FilterForm
          mode="create"
          onSubmit={handleCreate}
          onCancel={() => setIsCreating(false)}
        />
      )}

      {error ? (
        <ErrorState onRetry={() => mutate()} />
      ) : isLoading && !data ? (
        <SkeletonList />
      ) : data && data.length > 0 ? (
        <ul className="space-y-3">
          {data.map((filter) =>
            editingId === filter.id ? (
              <li key={filter.id}>
                <FilterForm
                  mode="edit"
                  initial={filter}
                  onSubmit={(payload) => handleReplace(filter.id, payload)}
                  onCancel={() => setEditingId(null)}
                />
              </li>
            ) : (
              <li key={filter.id}>
                <FilterCard
                  filter={filter}
                  onEdit={() => {
                    setEditingId(filter.id);
                    setIsCreating(false);
                  }}
                  onToggle={() => handleToggleEnabled(filter)}
                  onDelete={() => handleDelete(filter)}
                />
              </li>
            ),
          )}
        </ul>
      ) : !isCreating ? (
        <EmptyState onCreate={() => setIsCreating(true)} />
      ) : null}
    </div>
  );
}

function FilterCard({
  filter,
  onEdit,
  onToggle,
  onDelete,
}: {
  filter: FilterProfile;
  onEdit: () => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const summary = summarise(filter);
  return (
    <article className="border border-bone/10 bg-ink-soft/40 p-5">
      <div className="flex items-start justify-between gap-4 mb-3">
        <div>
          <h2 className="font-display text-lg text-bone">{filter.name}</h2>
          <p className="text-xs font-mono uppercase tracking-wider text-bone/40 mt-1">
            matched: {filter.match_count} tender{filter.match_count === 1 ? "" : "s"}
          </p>
        </div>
        <EnabledToggle enabled={filter.enabled} onChange={onToggle} />
      </div>

      {summary.length > 0 ? (
        <ul className="text-sm text-bone/70 space-y-1 mb-4">
          {summary.map((line) => (
            <li key={line.label} className="flex gap-2">
              <span className="text-[10px] font-mono uppercase tracking-wider text-bone/40 min-w-[80px] pt-1">
                {line.label}
              </span>
              <span className="font-mono text-xs">{line.value}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-bone/40 italic mb-4">No criteria — matches every tender.</p>
      )}

      <div className="flex gap-2 pt-3 border-t border-bone/10">
        <button
          type="button"
          onClick={onEdit}
          className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 border border-bone/30 text-bone/70 hover:border-bone hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
        >
          edit
        </button>
        <button
          type="button"
          onClick={onDelete}
          aria-label={`Delete filter ${filter.name}`}
          className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 border border-oxblood/60 text-oxblood hover:bg-oxblood hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-oxblood"
        >
          delete
        </button>
      </div>
    </article>
  );
}

function EnabledToggle({
  enabled,
  onChange,
}: {
  enabled: boolean;
  onChange: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label={enabled ? "Disable filter" : "Enable filter"}
      onClick={onChange}
      className={`relative inline-flex h-6 w-11 items-center transition focus:outline-none focus:ring-1 focus:ring-rust ${
        enabled ? "bg-sage/60" : "bg-bone/15"
      }`}
    >
      <span
        className={`inline-block h-4 w-4 transform bg-bone transition ${
          enabled ? "translate-x-6" : "translate-x-1"
        }`}
      />
    </button>
  );
}

function summarise(filter: FilterProfile): { label: string; value: string }[] {
  const out: { label: string; value: string }[] = [];
  const cpv = [
    ...(filter.cpv_codes ?? []),
    ...((filter.cpv_prefixes ?? []).map((p) => `${p}*`)),
  ];
  if (cpv.length) out.push({ label: "cpv", value: cpv.join(", ") });
  if (filter.keywords_any?.length)
    out.push({ label: "any of", value: filter.keywords_any.join(", ") });
  if (filter.keywords_all?.length)
    out.push({ label: "all of", value: filter.keywords_all.join(", ") });
  if (filter.keywords_none?.length)
    out.push({ label: "none of", value: filter.keywords_none.join(", ") });
  if (filter.buyer_names?.length)
    out.push({ label: "buyers", value: filter.buyer_names.join(", ") });
  if (filter.regions?.length)
    out.push({ label: "regions", value: filter.regions.join(", ") });
  if (filter.countries?.length)
    out.push({ label: "countries", value: filter.countries.join(", ") });
  if (filter.notice_types?.length)
    out.push({ label: "notices", value: filter.notice_types.join(", ") });
  if (filter.value_min || filter.value_max) {
    const lo = filter.value_min ? fmtVal(filter.value_min) : "0";
    const hi = filter.value_max ? fmtVal(filter.value_max) : "∞";
    out.push({ label: "value", value: `${lo}–${hi}` });
  }
  if (filter.min_days_to_deadline != null)
    out.push({ label: "deadline", value: `≥ ${filter.min_days_to_deadline}d` });
  return out;
}

function fmtVal(v: string | number): string {
  const n = typeof v === "string" ? Number(v) : v;
  if (!Number.isFinite(n)) return String(v);
  if (n >= 1_000_000) return `£${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `£${Math.round(n / 1_000)}k`;
  return `£${n}`;
}

function SkeletonList() {
  return (
    <ul className="space-y-3" aria-busy="true" aria-label="Loading filters">
      {Array.from({ length: 3 }).map((_, i) => (
        <li
          key={i}
          className="border border-bone/10 bg-ink-soft/30 p-5 animate-pulse"
        >
          <div className="h-5 w-1/3 bg-bone/10 mb-3" />
          <div className="h-3 w-2/3 bg-bone/10 mb-2" />
          <div className="h-3 w-1/2 bg-bone/5" />
        </li>
      ))}
    </ul>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="border border-bone/10 bg-ink-soft/30 p-8 text-center space-y-3">
      <h2 className="font-display text-xl text-bone">No filter profiles yet</h2>
      <p className="text-sm text-bone/60 max-w-md mx-auto">
        Filters define which tenders are worth a push notification. Set criteria
        like CPV codes, keywords, buyer region, or contract value — anything that
        characterises a winnable opportunity for you.
      </p>
      <div className="pt-2">
        <button
          type="button"
          onClick={onCreate}
          className="inline-block text-xs font-mono uppercase tracking-wider px-4 py-2 border border-rust text-rust hover:bg-rust hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
        >
          + create your first filter
        </button>
      </div>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div role="alert" className="border border-oxblood/60 bg-oxblood/10 p-6 space-y-3">
      <h2 className="font-display text-lg text-bone">Couldn't load filters</h2>
      <p className="text-sm text-bone/70">
        The backend at <span className="font-mono text-bone/90">/filters</span> didn't
        respond.
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

function formatError(err: unknown): string {
  if (err instanceof ApiError) {
    const detail =
      err.detail && typeof err.detail === "object" && "detail" in err.detail
        ? String((err.detail as { detail: unknown }).detail)
        : err.message;
    return `${err.status}: ${detail}`;
  }
  return err instanceof Error ? err.message : "Unknown error";
}

// ---------------------------------------------------------------------------
// Inline form (create + edit share the same shape).
// ---------------------------------------------------------------------------

function FilterForm({
  mode,
  initial,
  onSubmit,
  onCancel,
}: {
  mode: "create" | "edit";
  initial?: FilterProfile;
  onSubmit: (payload: FilterProfileCreate) => void | Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);
  const [cpvCodes, setCpvCodes] = useState(joinList(initial?.cpv_codes));
  const [cpvPrefixes, setCpvPrefixes] = useState(joinList(initial?.cpv_prefixes));
  const [keywordsAny, setKeywordsAny] = useState(joinList(initial?.keywords_any));
  const [keywordsAll, setKeywordsAll] = useState(joinList(initial?.keywords_all));
  const [keywordsNone, setKeywordsNone] = useState(joinList(initial?.keywords_none));
  const [buyerNames, setBuyerNames] = useState(joinList(initial?.buyer_names));
  const [regions, setRegions] = useState(joinList(initial?.regions));
  const [countries, setCountries] = useState(joinList(initial?.countries));
  const [noticeTypes, setNoticeTypes] = useState(joinList(initial?.notice_types));
  const [valueMin, setValueMin] = useState(asString(initial?.value_min));
  const [valueMax, setValueMax] = useState(asString(initial?.value_max));
  const [minDaysToDeadline, setMinDaysToDeadline] = useState(
    initial?.min_days_to_deadline != null ? String(initial.min_days_to_deadline) : "",
  );
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      const payload: FilterProfileCreate = {
        name: name.trim(),
        enabled,
        cpv_codes: splitList(cpvCodes),
        cpv_prefixes: splitList(cpvPrefixes),
        keywords_any: splitList(keywordsAny),
        keywords_all: splitList(keywordsAll),
        keywords_none: splitList(keywordsNone),
        buyer_names: splitList(buyerNames),
        regions: splitList(regions),
        countries: splitList(countries),
        notice_types: splitList(noticeTypes),
        value_min: parseDecimalOrNull(valueMin),
        value_max: parseDecimalOrNull(valueMax),
        min_days_to_deadline: parseIntOrNull(minDaysToDeadline),
      };
      await onSubmit(payload);
    } finally {
      setBusy(false);
    }
  };

  const fieldClass =
    "w-full bg-ink border border-bone/20 px-3 py-2 text-sm font-mono text-bone placeholder:text-bone/30 focus:outline-none focus:border-rust";
  const labelClass = "block space-y-1";
  const captionClass = "text-[10px] font-mono uppercase tracking-wider text-bone/40";

  return (
    <form
      onSubmit={submit}
      className="border border-rust/40 bg-ink-soft/60 p-5 space-y-4"
      aria-label={mode === "create" ? "Create filter profile" : "Edit filter profile"}
    >
      <header>
        <h2 className="font-display text-lg text-bone">
          {mode === "create" ? "New filter profile" : `Editing: ${initial?.name}`}
        </h2>
      </header>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <label className={labelClass}>
          <span className={captionClass}>name *</span>
          <input
            type="text"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={fieldClass}
            placeholder="e.g. South-West cleaning contracts"
          />
        </label>

        <label className="flex items-center gap-2 mt-6">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="accent-rust h-4 w-4"
          />
          <span className="text-xs font-mono uppercase tracking-wider text-bone/70">
            enabled (deliver pushes)
          </span>
        </label>
      </div>

      <fieldset className="space-y-3">
        <legend className={captionClass}>cpv classification</legend>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <ListField
            label="exact cpv codes"
            placeholder="90910000, 79993100"
            value={cpvCodes}
            onChange={setCpvCodes}
          />
          <ListField
            label="cpv prefixes (909*, 7999*)"
            placeholder="909, 7999"
            value={cpvPrefixes}
            onChange={setCpvPrefixes}
          />
        </div>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className={captionClass}>keywords</legend>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <ListField label="any of" placeholder="cleaning, janitorial" value={keywordsAny} onChange={setKeywordsAny} />
          <ListField label="all of" placeholder="schools, weekly" value={keywordsAll} onChange={setKeywordsAll} />
          <ListField label="none of" placeholder="construction" value={keywordsNone} onChange={setKeywordsNone} />
        </div>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className={captionClass}>buyer + geography</legend>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <ListField label="buyer names" placeholder="Bristol City Council" value={buyerNames} onChange={setBuyerNames} />
          <ListField label="notice types" placeholder="contract, prior" value={noticeTypes} onChange={setNoticeTypes} />
          <ListField label="regions" placeholder="South West, Wales" value={regions} onChange={setRegions} />
          <ListField label="countries (ISO)" placeholder="GB, IE" value={countries} onChange={setCountries} />
        </div>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className={captionClass}>value + deadline</legend>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <label className={labelClass}>
            <span className={captionClass}>value min (£)</span>
            <input
              type="number"
              inputMode="decimal"
              min="0"
              step="any"
              value={valueMin}
              onChange={(e) => setValueMin(e.target.value)}
              className={fieldClass}
              placeholder="50000"
            />
          </label>
          <label className={labelClass}>
            <span className={captionClass}>value max (£)</span>
            <input
              type="number"
              inputMode="decimal"
              min="0"
              step="any"
              value={valueMax}
              onChange={(e) => setValueMax(e.target.value)}
              className={fieldClass}
              placeholder="500000"
            />
          </label>
          <label className={labelClass}>
            <span className={captionClass}>min days to deadline</span>
            <input
              type="number"
              inputMode="numeric"
              min="0"
              step="1"
              value={minDaysToDeadline}
              onChange={(e) => setMinDaysToDeadline(e.target.value)}
              className={fieldClass}
              placeholder="14"
            />
          </label>
        </div>
      </fieldset>

      <div className="flex gap-2 pt-3 border-t border-bone/10">
        <button
          type="submit"
          disabled={busy || !name.trim()}
          className="text-xs font-mono uppercase tracking-wider px-4 py-2 border border-rust text-rust hover:bg-rust hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-rust disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? "saving…" : mode === "create" ? "create filter" : "save changes"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="text-xs font-mono uppercase tracking-wider px-4 py-2 border border-bone/30 text-bone/70 hover:border-bone hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-rust disabled:opacity-40"
        >
          cancel
        </button>
      </div>
    </form>
  );
}

function ListField({
  label,
  placeholder,
  value,
  onChange,
}: {
  label: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-[10px] font-mono uppercase tracking-wider text-bone/40">
        {label}
      </span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-ink border border-bone/20 px-3 py-2 text-sm font-mono text-bone placeholder:text-bone/30 focus:outline-none focus:border-rust"
      />
      <span className="text-[10px] font-mono text-bone/30">comma-separated</span>
    </label>
  );
}

function joinList(values: string[] | null | undefined): string {
  return values?.join(", ") ?? "";
}

function splitList(raw: string): string[] | null {
  const items = raw
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
  return items.length ? items : null;
}

function parseDecimalOrNull(raw: string): number | null {
  const t = raw.trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

function parseIntOrNull(raw: string): number | null {
  const t = raw.trim();
  if (!t) return null;
  const n = parseInt(t, 10);
  return Number.isFinite(n) ? n : null;
}

function asString(v: string | number | null | undefined): string {
  if (v == null) return "";
  return String(v);
}
