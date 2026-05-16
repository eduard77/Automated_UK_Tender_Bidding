"use client";

import { useState } from "react";
import useSWR from "swr";

import PillKicker from "./PillKicker";
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
import { formatCurrencyCompact } from "@/lib/format";

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
    await mutate(
      (cur) =>
        (cur ?? []).map((f) => (f.id === filter.id ? { ...f, enabled: next } : f)),
      { revalidate: false },
    );
    try {
      await updateFilter(filter.id, { enabled: next });
    } catch (err) {
      setTopError(formatError(err));
      await mutate();
    }
  };

  const handleDelete = async (filter: FilterProfile) => {
    const ok = window.confirm(
      `Delete filter "${filter.name}"? This will also delete its match history.`,
    );
    if (!ok) return;
    await mutate(
      (cur) => (cur ?? []).filter((f) => f.id !== filter.id),
      { revalidate: false },
    );
    try {
      await deleteFilter(filter.id);
    } catch (err) {
      setTopError(formatError(err));
      await mutate();
    }
  };

  return (
    <div className="space-y-12 py-14">
      <header className="space-y-6">
        <PillKicker withDot={false}>Bid profiles</PillKicker>
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
          What you bid for.
        </h1>
        <p
          className="text-text-muted"
          style={{ fontSize: "17px", lineHeight: 1.6, maxWidth: "720px" }}
        >
          Filter profiles decide which tenders are surfaced to the top of your
          desk and which trigger a push notification. CPV codes, keywords,
          regions, contract value, deadline window — anything that
          characterises a winnable opportunity for you.
        </p>
        <div className="flex flex-wrap items-center gap-4 pt-2">
          {!isCreating && (
            <button
              type="button"
              onClick={() => {
                setIsCreating(true);
                setEditingId(null);
              }}
              className="btn-primary"
            >
              + New profile <span className="arrow">→</span>
            </button>
          )}
          <span className="text-text-dim" style={{ fontSize: "13px" }}>
            {data
              ? `${data.length} profile${data.length === 1 ? "" : "s"}`
              : isLoading
                ? "Loading…"
                : ""}
          </span>
        </div>
      </header>

      {topError && (
        <div
          role="alert"
          className="rounded-xl border border-danger/40 bg-danger/5 p-5 text-text"
          style={{ borderRadius: "18px", fontSize: "14px" }}
        >
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
        <ul className="space-y-4">
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

// ---------------------------------------------------------------------------
// Filter card
// ---------------------------------------------------------------------------

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
  return (
    <article
      className="rounded-2xl border border-border-strong p-8 backdrop-blur-md"
      style={{
        background: "rgba(17, 23, 32, 0.82)",
        borderRadius: "18px",
      }}
    >
      <div className="flex flex-wrap items-start justify-between gap-6 mb-6">
        <div className="flex-1 min-w-0">
          <h2
            className="font-display text-text"
            style={{
              fontSize: "22px",
              fontWeight: 400,
              letterSpacing: "-0.02em",
              lineHeight: 1.2,
            }}
          >
            {filter.name}
          </h2>
          <div
            className="mt-2 flex flex-wrap items-center gap-3 text-text-muted"
            style={{ fontSize: "13px" }}
          >
            <span className="inline-flex items-center gap-2">
              <span
                className="display-num text-mint-pale"
                style={{ fontSize: "16px" }}
              >
                {filter.match_count}
              </span>{" "}
              match{filter.match_count === 1 ? "" : "es"}
            </span>
          </div>
        </div>
        <EnabledToggle enabled={filter.enabled} onChange={onToggle} />
      </div>

      <FilterSummary filter={filter} />

      <div className="mt-6 flex flex-wrap gap-3 border-t border-border pt-5">
        <button type="button" onClick={onEdit} className="btn-ghost">
          Edit
        </button>
        <button
          type="button"
          onClick={onDelete}
          className="btn-danger"
          aria-label={`Delete filter ${filter.name}`}
        >
          Delete
        </button>
      </div>
    </article>
  );
}

function FilterSummary({ filter }: { filter: FilterProfile }) {
  const cpv = [
    ...(filter.cpv_codes ?? []),
    ...((filter.cpv_prefixes ?? []).map((p) => `${p}*`)),
  ];
  const tags: { label: string; values: string[]; mono?: boolean }[] = [];
  if (cpv.length) tags.push({ label: "CPV", values: cpv, mono: true });
  if (filter.keywords_any?.length)
    tags.push({ label: "Any of", values: filter.keywords_any });
  if (filter.keywords_all?.length)
    tags.push({ label: "All of", values: filter.keywords_all });
  if (filter.keywords_none?.length)
    tags.push({ label: "None of", values: filter.keywords_none });
  if (filter.buyer_names?.length)
    tags.push({ label: "Buyers", values: filter.buyer_names });
  if (filter.regions?.length)
    tags.push({ label: "Regions", values: filter.regions });
  if (filter.countries?.length)
    tags.push({ label: "Countries", values: filter.countries });
  if (filter.notice_types?.length)
    tags.push({ label: "Notices", values: filter.notice_types });
  if (filter.value_min || filter.value_max) {
    const lo = filter.value_min ? formatCurrencyCompact(filter.value_min, "GBP") : "£0";
    const hi = filter.value_max ? formatCurrencyCompact(filter.value_max, "GBP") : "∞";
    tags.push({ label: "Value", values: [`${lo}–${hi}`] });
  }
  if (filter.min_days_to_deadline != null)
    tags.push({
      label: "Deadline",
      values: [`≥ ${filter.min_days_to_deadline} days`],
    });

  if (tags.length === 0) {
    return (
      <p className="italic text-text-dim" style={{ fontSize: "14px" }}>
        No criteria — matches every tender.
      </p>
    );
  }

  return (
    <dl className="space-y-3">
      {tags.map((row) => (
        <div key={row.label} className="flex flex-wrap items-baseline gap-3">
          <dt
            className="text-text-dim shrink-0"
            style={{
              fontSize: "11px",
              letterSpacing: "0.12em",
              textTransform: "uppercase",
              fontWeight: 500,
              minWidth: "84px",
            }}
          >
            {row.label}
          </dt>
          <dd className="flex flex-wrap gap-1.5">
            {row.values.map((v) => (
              <span
                key={v}
                className={row.mono ? "font-mono" : ""}
                style={{
                  fontSize: row.mono ? "11px" : "13px",
                  padding: "3px 8px",
                  background: "rgba(255, 255, 255, 0.04)",
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                  borderRadius: row.mono ? "4px" : "6px",
                  color: "rgba(255, 255, 255, 0.78)",
                  letterSpacing: row.mono ? "0.02em" : "0",
                }}
              >
                {v}
              </span>
            ))}
          </dd>
        </div>
      ))}
    </dl>
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
      className={`relative inline-flex h-7 w-12 shrink-0 items-center rounded-full border transition-colors ${
        enabled
          ? "border-mint/60 bg-mint/30"
          : "border-border-strong bg-bg-elevated"
      }`}
    >
      <span
        className={`inline-block h-5 w-5 transform rounded-full transition-transform ${
          enabled
            ? "translate-x-[24px] bg-mint"
            : "translate-x-1 bg-text-muted"
        }`}
      />
    </button>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

function SkeletonList() {
  return (
    <ul className="space-y-4" aria-busy="true" aria-label="Loading filter profiles">
      {Array.from({ length: 3 }).map((_, i) => (
        <li
          key={i}
          className="shimmer rounded-2xl"
          style={{ height: "200px", borderRadius: "18px" }}
        />
      ))}
    </ul>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-14 text-center backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <svg
        width="90"
        height="90"
        viewBox="0 0 100 100"
        className="mx-auto mb-6 text-text-dim"
        fill="none"
        stroke="currentColor"
        strokeWidth="0.8"
        aria-hidden="true"
      >
        <rect x="20" y="25" width="60" height="50" />
        <line x1="20" y1="40" x2="80" y2="40" />
        <line x1="35" y1="55" x2="65" y2="55" />
        <line x1="40" y1="65" x2="60" y2="65" />
      </svg>
      <h2
        className="font-display text-text mb-4"
        style={{ fontSize: "28px", letterSpacing: "-0.02em" }}
      >
        No bid profiles yet.
      </h2>
      <p
        className="text-text-muted mb-7 mx-auto max-w-md"
        style={{ fontSize: "15px", lineHeight: 1.55 }}
      >
        A profile is how you tell the system what a winnable tender looks like:
        which CPV codes, which regions, which deadline window. Add one and the
        next poll will surface matches.
      </p>
      <button type="button" onClick={onCreate} className="btn-primary">
        + Create your first profile <span className="arrow">→</span>
      </button>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="rounded-2xl border border-danger/40 bg-bg-elevated p-10 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <h2 className="font-display text-text" style={{ fontSize: "24px" }}>
        Couldn&apos;t load filters
      </h2>
      <p className="text-text-muted mt-3" style={{ fontSize: "15px" }}>
        The backend at <code className="font-mono text-text">/filters</code>{" "}
        didn&apos;t respond.
      </p>
      <button type="button" onClick={onRetry} className="btn-secondary mt-6">
        ↻ Retry
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
// Filter form (create + edit share the same shape)
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
    initial?.min_days_to_deadline != null
      ? String(initial.min_days_to_deadline)
      : "",
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

  return (
    <form
      onSubmit={submit}
      className="rounded-2xl border border-mint/30 p-8 backdrop-blur-md space-y-8"
      style={{
        borderRadius: "24px",
        background: "rgba(17, 23, 32, 0.92)",
      }}
      aria-label={mode === "create" ? "Create filter profile" : "Edit filter profile"}
    >
      <header className="flex flex-wrap items-baseline justify-between gap-4">
        <h2
          className="font-display text-text"
          style={{ fontSize: "24px", letterSpacing: "-0.02em" }}
        >
          {mode === "create" ? "New profile" : `Editing: ${initial?.name}`}
        </h2>
        <label className="flex items-center gap-3 text-text-muted text-[13px]">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="h-4 w-4 cursor-pointer accent-mint"
          />
          Enabled (deliver pushes)
        </label>
      </header>

      <FieldGroup>
        <Field
          label="Profile name"
          required
          value={name}
          onChange={setName}
          placeholder="Construction — broad"
        />
      </FieldGroup>

      <Fieldset legend="CPV classification">
        <FieldGroup cols={2}>
          <Field
            label="Exact CPV codes"
            value={cpvCodes}
            onChange={setCpvCodes}
            placeholder="45000000, 45210000"
            mono
            help="Comma-separated"
          />
          <Field
            label="CPV prefixes"
            value={cpvPrefixes}
            onChange={setCpvPrefixes}
            placeholder="45, 4521"
            mono
            help="Match any code starting with this prefix"
          />
        </FieldGroup>
      </Fieldset>

      <Fieldset legend="Keywords">
        <FieldGroup cols={3}>
          <Field
            label="Any of"
            value={keywordsAny}
            onChange={setKeywordsAny}
            placeholder="cleaning, janitorial"
            help="At least one keyword must appear"
          />
          <Field
            label="All of"
            value={keywordsAll}
            onChange={setKeywordsAll}
            placeholder="schools, weekly"
            help="Every keyword must appear"
          />
          <Field
            label="None of"
            value={keywordsNone}
            onChange={setKeywordsNone}
            placeholder="demolition"
            help="Reject if any appear"
          />
        </FieldGroup>
      </Fieldset>

      <Fieldset legend="Buyer + geography">
        <FieldGroup cols={2}>
          <Field
            label="Buyer names"
            value={buyerNames}
            onChange={setBuyerNames}
            placeholder="Bristol City Council"
          />
          <Field
            label="Notice types"
            value={noticeTypes}
            onChange={setNoticeTypes}
            placeholder="contract, prior"
          />
          <Field
            label="Regions"
            value={regions}
            onChange={setRegions}
            placeholder="South West, Wales"
          />
          <Field
            label="Countries (ISO)"
            value={countries}
            onChange={setCountries}
            placeholder="GB, IE"
          />
        </FieldGroup>
      </Fieldset>

      <Fieldset legend="Value + deadline">
        <FieldGroup cols={3}>
          <NumberField
            label="Value min"
            value={valueMin}
            onChange={setValueMin}
            placeholder="50000"
            prefix="£"
          />
          <NumberField
            label="Value max"
            value={valueMax}
            onChange={setValueMax}
            placeholder="500000"
            prefix="£"
          />
          <NumberField
            label="Min days to deadline"
            value={minDaysToDeadline}
            onChange={setMinDaysToDeadline}
            placeholder="14"
          />
        </FieldGroup>
      </Fieldset>

      <div className="flex flex-wrap gap-3 border-t border-border pt-6">
        <button
          type="submit"
          disabled={busy || !name.trim()}
          className="btn-primary"
        >
          {busy ? "Saving…" : mode === "create" ? "Create profile" : "Save changes"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="btn-secondary"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function Fieldset({
  legend,
  children,
}: {
  legend: string;
  children: React.ReactNode;
}) {
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

function FieldGroup({
  cols = 1,
  children,
}: {
  cols?: 1 | 2 | 3;
  children: React.ReactNode;
}) {
  const grid =
    cols === 1
      ? "grid grid-cols-1 gap-5"
      : cols === 2
        ? "grid grid-cols-1 gap-5 md:grid-cols-2"
        : "grid grid-cols-1 gap-5 md:grid-cols-3";
  return <div className={grid}>{children}</div>;
}

function Field({
  label,
  required,
  value,
  onChange,
  placeholder,
  mono,
  help,
}: {
  label: string;
  required?: boolean;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  help?: string;
}) {
  return (
    <label className="block">
      <FieldLabel>
        {label}
        {required && <span className="text-mint"> *</span>}
      </FieldLabel>
      <input
        type="text"
        required={required}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`mt-2 w-full rounded-md border border-border-strong bg-bg-elevated px-3.5 py-2.5 text-text placeholder:text-text-dim transition-colors focus:border-mint focus:outline-none ${mono ? "font-mono text-[13px]" : "text-[14px]"}`}
      />
      {help && (
        <span
          className="mt-2 block text-text-dim"
          style={{
            fontSize: "11px",
            letterSpacing: "0.04em",
          }}
        >
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

// ---------------------------------------------------------------------------
// Form helpers
// ---------------------------------------------------------------------------

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
