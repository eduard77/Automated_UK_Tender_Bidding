"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import useSWR from "swr";

import PillKicker from "./PillKicker";
import PortalStatusBadge from "./PortalStatusBadge";
import StatBlock from "./StatBlock";
import {
  type AdapterStatus,
  type LoginType,
  type PlatformUpdate,
  type PortalPlatformDetail,
  fetcher,
  updatePlatform,
} from "@/lib/api";

const LOGIN_OPTIONS: LoginType[] = [
  "none",
  "email_only",
  "username_password",
  "2fa",
  "oauth",
  "unknown",
];
const STATUS_OPTIONS: AdapterStatus[] = [
  "not_started",
  "stub",
  "read_only",
  "full",
  "deprecated",
];

export default function PlatformDetail({ slug }: { slug: string }) {
  const { data, error, isLoading, mutate } = useSWR<PortalPlatformDetail>(
    `/platforms/${slug}`,
    fetcher,
    { revalidateOnFocus: false },
  );

  if (error) {
    return <div className="py-14 text-text">Failed to load platform.</div>;
  }
  if (isLoading || !data) {
    return (
      <div className="py-14 text-text-dim" style={{ fontSize: "14px" }}>
        Loading…
      </div>
    );
  }
  return <Inner platform={data} onSaved={() => mutate()} />;
}

function Inner({
  platform,
  onSaved,
}: {
  platform: PortalPlatformDetail;
  onSaved: () => void;
}) {
  return (
    <div className="space-y-12 py-14">
      <header className="space-y-5">
        <PillKicker withDot={false}>Platform · adapter target</PillKicker>
        <div className="flex flex-wrap items-baseline gap-3 text-text-dim" style={{ fontSize: "13px" }}>
          <span style={{ letterSpacing: "0.1em", textTransform: "uppercase" }}>
            {platform.vendor}
          </span>
          <span aria-hidden="true">·</span>
          <span>{platform.kind}</span>
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
          {platform.display_name}
        </h1>
        <div>
          <Link
            href="/platforms"
            className="text-text-muted hover:text-text"
            style={{ fontSize: "13px" }}
          >
            ← All platforms
          </Link>
        </div>
      </header>

      <div className="flex flex-wrap gap-x-14 gap-y-6 border-y border-border py-8">
        <StatBlock
          label="Total tenders"
          value={platform.total_tender_count.toString()}
          unit="seen"
          tone="mint"
        />
        <StatBlock
          label="Buyer instances"
          value={platform.buyer_instance_count.toString()}
          unit={platform.buyer_instance_count === 1 ? "portal" : "portals"}
        />
        <StatBlock label="Adapter" value={<PortalStatusBadge status={platform.adapter_status} />} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <section className="lg:col-span-2 space-y-8">
          <LinkedPortals platform={platform} />
          <SampleUrls platform={platform} />
        </section>
        <aside>
          <AdminPanel platform={platform} onSaved={onSaved} />
        </aside>
      </div>
    </div>
  );
}

function LinkedPortals({ platform }: { platform: PortalPlatformDetail }) {
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md"
      style={{ borderRadius: "18px" }}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-4 mb-5">
        <h2
          className="font-display text-text"
          style={{ fontSize: "22px", letterSpacing: "-0.02em" }}
        >
          Linked buyer portals
        </h2>
        <Link
          href={`/portals?platform_id=${platform.id}`}
          className="text-text-muted hover:text-text"
          style={{ fontSize: "13px" }}
        >
          Build adapter →
        </Link>
      </div>
      {platform.portals.length === 0 ? (
        <p className="text-text-dim" style={{ fontSize: "14px" }}>
          No portals linked yet.
        </p>
      ) : (
        <table className="w-full text-left" style={{ fontSize: "13px" }}>
          <thead
            className="text-text-dim"
            style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
          >
            <tr>
              <th className="pb-3 pr-3 font-normal">Domain</th>
              <th className="pb-3 pr-3 font-normal">Name</th>
              <th className="pb-3 pr-3 font-normal">Tenders</th>
              <th className="pb-3 font-normal">Adapter</th>
            </tr>
          </thead>
          <tbody>
            {platform.portals.map((p) => (
              <tr key={p.id} className="border-t border-border/40">
                <td className="py-3 pr-3">
                  <Link
                    href={`/portals/${p.id}`}
                    className="font-mono text-text hover:text-mint-pale"
                    style={{ fontSize: "12px" }}
                  >
                    {p.domain}
                  </Link>
                </td>
                <td className="py-3 pr-3 text-text-muted">{p.display_name}</td>
                <td className="py-3 pr-3 text-mint-pale">{p.tender_count}</td>
                <td className="py-3">
                  <PortalStatusBadge status={p.adapter_status} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function SampleUrls({ platform }: { platform: PortalPlatformDetail }) {
  if (platform.sample_tender_urls.length === 0) return null;
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md"
      style={{ borderRadius: "18px" }}
    >
      <h2
        className="font-display text-text mb-4"
        style={{ fontSize: "22px", letterSpacing: "-0.02em" }}
      >
        Sample tender URLs
      </h2>
      <ul className="space-y-2">
        {platform.sample_tender_urls.map((u) => (
          <li
            key={u}
            className="font-mono text-text-muted break-all"
            style={{ fontSize: "12px" }}
          >
            {u}
          </li>
        ))}
      </ul>
    </div>
  );
}

function AdminPanel({
  platform,
  onSaved,
}: {
  platform: PortalPlatformDetail;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<PlatformUpdate>({
    adapter_status: platform.adapter_status,
    adapter_module: platform.adapter_module ?? "",
    login_type: platform.login_type,
    notes: platform.notes ?? "",
  });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    setForm({
      adapter_status: platform.adapter_status,
      adapter_module: platform.adapter_module ?? "",
      login_type: platform.login_type,
      notes: platform.notes ?? "",
    });
  }, [platform.slug, platform.adapter_status, platform.adapter_module, platform.login_type, platform.notes]);

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await updatePlatform(platform.slug, {
        ...form,
        adapter_module: form.adapter_module === "" ? null : form.adapter_module,
        notes: form.notes === "" ? null : form.notes,
      });
      setMsg("Saved.");
      onSaved();
    } catch {
      setMsg("Save failed.");
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

      <Field label="Adapter status">
        <select
          value={form.adapter_status}
          onChange={(e) =>
            setForm({ ...form, adapter_status: e.target.value as AdapterStatus })
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

      <Field label="Adapter module">
        <input
          type="text"
          value={form.adapter_module ?? ""}
          onChange={(e) => setForm({ ...form, adapter_module: e.target.value })}
          placeholder="tender_agent.services.portals.…"
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60 font-mono"
          style={{ fontSize: "12px" }}
        />
      </Field>

      <Field label="Notes">
        <textarea
          value={form.notes ?? ""}
          onChange={(e) => setForm({ ...form, notes: e.target.value })}
          rows={4}
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-mint-pale/60 resize-none"
        />
      </Field>

      <button
        type="button"
        onClick={save}
        disabled={busy}
        className="btn-primary disabled:opacity-50"
      >
        {busy ? "Saving…" : "Save changes"}
      </button>
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
