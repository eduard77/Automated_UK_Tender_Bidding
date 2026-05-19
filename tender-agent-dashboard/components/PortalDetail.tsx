"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import useSWR, { mutate as globalMutate } from "swr";

import ClassificationDisplay from "./ClassificationDisplay";
import PillKicker from "./PillKicker";
import PortalPriorityBadge from "./PortalPriorityBadge";
import PortalStatusBadge from "./PortalStatusBadge";
import {
  type AdapterStatus,
  type LoginType,
  type PortalDetail as PortalDetailType,
  type PortalUpdate,
  type Priority,
  deprecatePortal,
  fetcher,
  reclassifyPortal,
  updatePortal,
} from "@/lib/api";
import { relativeAge } from "@/lib/format";

const LOGIN_OPTIONS: LoginType[] = [
  "none",
  "email_only",
  "username_password",
  "2fa",
  "oauth",
  "unknown",
];
const PRIORITY_OPTIONS: Priority[] = ["critical", "high", "medium", "low"];
const STATUS_OPTIONS: AdapterStatus[] = [
  "not_started",
  "stub",
  "read_only",
  "full",
  "deprecated",
];

export default function PortalDetail({ id }: { id: number }) {
  const path = `/portals/${id}`;
  const { data, error, isLoading, mutate } = useSWR<PortalDetailType>(
    path,
    fetcher,
    { revalidateOnFocus: false },
  );

  if (error) {
    return (
      <div className="py-14 text-text">
        <p>Failed to load portal.</p>
      </div>
    );
  }
  if (isLoading || !data) {
    return (
      <div className="py-14 text-text-dim" style={{ fontSize: "14px" }}>
        Loading…
      </div>
    );
  }
  return (
    <DetailInner
      portal={data}
      onMutate={async () => {
        await mutate();
        await globalMutate(
          (key) => typeof key === "string" && key.startsWith("/portals"),
        );
      }}
    />
  );
}

function DetailInner({
  portal,
  onMutate,
}: {
  portal: PortalDetailType;
  onMutate: () => Promise<void>;
}) {
  return (
    <div className="space-y-12 py-14">
      <header className="space-y-5">
        <PillKicker withDot={false}>Portal · registry detail</PillKicker>
        <div className="flex flex-wrap items-baseline gap-4">
          <span
            className="font-mono text-text-muted"
            style={{
              fontSize: "12px",
              fontWeight: 500,
              padding: "4px 10px",
              background: "rgba(255, 255, 255, 0.04)",
              border: "1px solid rgba(255, 255, 255, 0.08)",
              borderRadius: "4px",
            }}
          >
            {portal.domain}
          </span>
        </div>
        <h1
          className="font-display text-text"
          style={{
            fontSize: "clamp(32px, 4vw, 52px)",
            fontWeight: 400,
            letterSpacing: "-0.03em",
            lineHeight: 1.05,
            fontVariationSettings: '"opsz" 96',
          }}
        >
          {portal.display_name}
        </h1>
        <div className="flex flex-wrap items-center gap-3 text-text-dim" style={{ fontSize: "13px" }}>
          <span>
            {portal.tender_count} tender{portal.tender_count === 1 ? "" : "s"}
          </span>
          <span aria-hidden="true">·</span>
          <span>First seen {relativeAge(portal.first_seen_at) ?? "—"}</span>
          <span aria-hidden="true">·</span>
          <span>Last seen {relativeAge(portal.last_seen_at) ?? "—"}</span>
          <span aria-hidden="true">·</span>
          <PortalPriorityBadge priority={portal.priority} />
          <PortalStatusBadge status={portal.adapter_status} />
        </div>
        <div>
          <Link
            href="/portals"
            className="text-text-muted hover:text-text"
            style={{ fontSize: "13px" }}
          >
            ← All portals
          </Link>
        </div>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <section className="lg:col-span-2 space-y-6">
          <ClassificationDisplay portal={portal} />
          <ReclassifyControls portal={portal} onMutate={onMutate} />
          <RecentSightings portal={portal} />
        </section>
        <aside>
          <AdminPanel portal={portal} onMutate={onMutate} />
        </aside>
      </div>
    </div>
  );
}

function ReclassifyControls({
  portal,
  onMutate,
}: {
  portal: PortalDetailType;
  onMutate: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const trigger = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const res = await reclassifyPortal(portal.id);
      setMsg(
        res.queued
          ? "Classification queued. The portal record will update once Claude responds."
          : `Classification finished with status: ${res.status}.`,
      );
      await onMutate();
    } catch {
      setMsg("Re-classify failed.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md"
      style={{ borderRadius: "18px" }}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-4 mb-3">
        <h3
          className="font-display text-text"
          style={{ fontSize: "20px", letterSpacing: "-0.015em" }}
        >
          Re-classify this portal
        </h3>
        <button
          type="button"
          onClick={trigger}
          disabled={busy}
          className="btn-primary disabled:opacity-50"
        >
          {busy ? "Re-classifying…" : "Re-classify now"}
        </button>
      </div>
      <p className="text-text-muted" style={{ fontSize: "13px", lineHeight: 1.55 }}>
        Useful after the portal redesigns its homepage, or after a previously
        failed fetch. The job runs in the background.
      </p>
      {msg && (
        <p className="text-mint-pale mt-4" style={{ fontSize: "13px" }}>
          {msg}
        </p>
      )}
    </div>
  );
}

function RecentSightings({ portal }: { portal: PortalDetailType }) {
  if (portal.recent_sightings.length === 0) {
    return null;
  }
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md"
      style={{ borderRadius: "18px" }}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-4 mb-5">
        <h3
          className="font-display text-text"
          style={{ fontSize: "20px", letterSpacing: "-0.015em" }}
        >
          Recent sightings
        </h3>
        <Link
          href={`/portals/${portal.id}/sightings`}
          className="text-text-muted hover:text-text"
          style={{ fontSize: "13px" }}
        >
          View all →
        </Link>
      </div>
      <table className="w-full text-left" style={{ fontSize: "13px" }}>
        <thead className="text-text-dim" style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}>
          <tr>
            <th className="pb-3 font-normal">Tender</th>
            <th className="pb-3 font-normal">Source</th>
            <th className="pb-3 font-normal">Kind</th>
            <th className="pb-3 font-normal">When</th>
          </tr>
        </thead>
        <tbody>
          {portal.recent_sightings.map((s) => (
            <tr key={s.id} className="border-t border-border/40">
              <td className="py-3 pr-3 text-text truncate" style={{ maxWidth: "300px" }}>
                <Link
                  href={`/tenders/${s.tender_id}`}
                  className="hover:text-mint-pale"
                >
                  {s.tender_title ?? `Tender #${s.tender_id}`}
                </Link>
              </td>
              <td className="py-3 pr-3 text-text-muted">{s.extracted_from}</td>
              <td className="py-3 pr-3 text-text-muted">{s.sighting_type}</td>
              <td className="py-3 text-text-dim">
                {relativeAge(s.extracted_at) ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AdminPanel({
  portal,
  onMutate,
}: {
  portal: PortalDetailType;
  onMutate: () => Promise<void>;
}) {
  const [form, setForm] = useState<PortalUpdate>({
    display_name: portal.display_name,
    login_type: portal.login_type,
    priority: portal.priority,
    adapter_status: portal.adapter_status,
    adapter_module: portal.adapter_module ?? "",
    notes: portal.notes ?? "",
  });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    setForm({
      display_name: portal.display_name,
      login_type: portal.login_type,
      priority: portal.priority,
      adapter_status: portal.adapter_status,
      adapter_module: portal.adapter_module ?? "",
      notes: portal.notes ?? "",
    });
  }, [portal.id, portal.display_name, portal.login_type, portal.priority, portal.adapter_status, portal.adapter_module, portal.notes]);

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await updatePortal(portal.id, {
        ...form,
        adapter_module: form.adapter_module === "" ? null : form.adapter_module,
        notes: form.notes === "" ? null : form.notes,
      });
      setMsg("Saved.");
      await onMutate();
    } catch {
      setMsg("Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const markDeprecated = async () => {
    if (
      !window.confirm(
        "This portal will be marked deprecated and no new sightings will count toward it. Existing sightings preserved.",
      )
    ) {
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await deprecatePortal(portal.id);
      setMsg("Marked deprecated.");
      await onMutate();
    } catch {
      setMsg("Action failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md space-y-5"
      style={{ borderRadius: "18px" }}
    >
      <h3
        className="font-display text-text"
        style={{ fontSize: "20px", letterSpacing: "-0.015em" }}
      >
        Admin overrides
      </h3>

      <Field label="Display name">
        <input
          type="text"
          value={form.display_name ?? ""}
          onChange={(e) => setForm({ ...form, display_name: e.target.value })}
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60"
        />
      </Field>

      <Field label="Login type">
        <select
          value={form.login_type}
          onChange={(e) =>
            setForm({ ...form, login_type: e.target.value as LoginType })
          }
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60"
        >
          {LOGIN_OPTIONS.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Priority">
        <select
          value={form.priority}
          onChange={(e) =>
            setForm({ ...form, priority: e.target.value as Priority })
          }
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60"
        >
          {PRIORITY_OPTIONS.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Adapter status">
        <select
          value={form.adapter_status}
          onChange={(e) =>
            setForm({
              ...form,
              adapter_status: e.target.value as AdapterStatus,
            })
          }
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60"
        >
          {STATUS_OPTIONS.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Adapter module">
        <input
          type="text"
          value={form.adapter_module ?? ""}
          onChange={(e) =>
            setForm({ ...form, adapter_module: e.target.value })
          }
          placeholder="tender_agent.adapters.portals.…"
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60 font-mono"
          style={{ fontSize: "12px" }}
        />
      </Field>

      <Field label="Notes">
        <textarea
          value={form.notes ?? ""}
          onChange={(e) => setForm({ ...form, notes: e.target.value })}
          rows={3}
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60 resize-none"
        />
      </Field>

      <div className="flex flex-wrap items-center gap-3 pt-2">
        <button
          type="button"
          onClick={save}
          disabled={busy}
          className="btn-primary disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save changes"}
        </button>
        <button
          type="button"
          onClick={markDeprecated}
          disabled={busy || portal.adapter_status === "deprecated"}
          className="ml-auto text-danger hover:text-text disabled:opacity-50"
          style={{ fontSize: "13px" }}
        >
          Mark deprecated
        </button>
      </div>
      {msg && (
        <p className="text-mint-pale" style={{ fontSize: "13px" }}>
          {msg}
        </p>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span
        className="text-text-dim block mb-1.5"
        style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
      >
        {label}
      </span>
      {children}
    </label>
  );
}
