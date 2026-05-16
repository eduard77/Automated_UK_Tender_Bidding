"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import useSWR from "swr";

import ClaimsDisplay from "./ClaimsDisplay";
import PillKicker from "./PillKicker";
import {
  ApiError,
  archiveVaultDocument,
  fetcher,
  isPlaceholderTitle,
  supersedeVaultDocument,
  updateVaultClaims,
  vaultCategoryLabel,
  type ClaimsRecord,
  type VaultDocument,
} from "@/lib/api";
import { daysUntil, formatDate, relativeAge } from "@/lib/format";

export default function VaultDocumentDetail({ id }: { id: number }) {
  const router = useRouter();
  const { data, error, mutate } = useSWR<VaultDocument>(
    `/vault/documents/${id}`,
    fetcher,
    { revalidateOnFocus: false },
  );

  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [topError, setTopError] = useState<string | null>(null);
  const superSedeRef = useRef<HTMLInputElement | null>(null);

  if (error instanceof ApiError && error.status === 404) {
    return <NotFound />;
  }
  if (error) {
    return <TopLevelError onRetry={() => mutate()} />;
  }
  if (!data) {
    return <DetailSkeleton />;
  }

  const version = data.current_version;

  const onArchive = async () => {
    const ok = window.confirm(
      `Archive "${data.title}"? Bids referencing this version will keep working; it disappears from the vault list.`,
    );
    if (!ok) return;
    setBusy(true);
    try {
      await archiveVaultDocument(data.id);
      router.push("/vault");
    } catch (err) {
      setTopError(err instanceof Error ? err.message : "Archive failed");
      setBusy(false);
    }
  };

  const onSupersede = async (file: File) => {
    setBusy(true);
    setTopError(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const updated = await supersedeVaultDocument(data.id, fd);
      await mutate(updated, { revalidate: false });
    } catch (err) {
      setTopError(err instanceof Error ? err.message : "Supersede failed");
    } finally {
      setBusy(false);
    }
  };

  const onClaimsSave = async (claims: ClaimsRecord) => {
    if (!version) return;
    setBusy(true);
    setTopError(null);
    try {
      const expiry = readField(claims, "valid_until") ?? readField(claims, "review_due");
      const issued =
        readField(claims, "valid_from") ??
        readField(claims, "issued_date") ??
        readField(claims, "signed_date");
      const issuingBody =
        readField(claims, "insurer") ??
        readField(claims, "certifying_body") ??
        readField(claims, "auditor") ??
        null;
      const updated = await updateVaultClaims(data.id, version.id, {
        claims,
        claims_confirmed: true,
        expiry_date: expiry,
        issued_date: issued,
        issuing_body: issuingBody,
      });
      await mutate(updated, { revalidate: false });
      setEditing(false);
    } catch (err) {
      setTopError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const isPlaceholder = isPlaceholderTitle(data.title);
  const expiry = version?.expiry_date ?? null;
  const days = expiry ? daysUntil(expiry) : null;
  const expiringSoon = days !== null && days >= 0 && days <= 90;
  const expired = days !== null && days < 0;

  return (
    <div className="space-y-12 py-14">
      <header className="space-y-6">
        <Link
          href="/vault"
          className="inline-flex items-center gap-2 text-[13px] text-text-muted hover:text-text"
        >
          ← Back to vault
        </Link>

        {isPlaceholder && (
          <div
            role="note"
            className="rounded-xl border p-4 text-text"
            style={{
              borderRadius: "12px",
              background: "rgba(245, 166, 35, 0.08)",
              borderColor: "rgba(245, 166, 35, 0.25)",
              fontSize: "13px",
              lineHeight: 1.55,
            }}
          >
            <strong className="text-amber">Placeholder.</strong> This document
            was seeded for dashboard testing. Replace with real evidence before
            relying on bid assessments.
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <span
            className="text-text"
            style={{
              fontSize: "11px",
              fontWeight: 500,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              padding: "4px 10px",
              background: "rgba(255, 255, 255, 0.06)",
              border: "1px solid rgba(255, 255, 255, 0.16)",
              borderRadius: "5px",
            }}
          >
            {vaultCategoryLabel(data.category)}
          </span>
          {data.subcategory && (
            <span
              className="text-text-muted"
              style={{
                fontSize: "12px",
                letterSpacing: "0.08em",
                textTransform: "uppercase",
              }}
            >
              {data.subcategory.replaceAll("_", " ")}
            </span>
          )}
          {expiringSoon && days !== null && (
            <PillKicker withDot={false}>
              <span className="text-amber">
                Expires in {days} day{days === 1 ? "" : "s"}
              </span>
            </PillKicker>
          )}
          {expired && (
            <span
              className="text-danger"
              style={{
                fontSize: "11px",
                fontWeight: 500,
                letterSpacing: "0.12em",
                textTransform: "uppercase",
              }}
            >
              Expired
            </span>
          )}
        </div>

        <h1
          className="font-display text-text text-balance"
          style={{
            fontSize: "clamp(36px, 5vw, 56px)",
            fontWeight: 400,
            letterSpacing: "-0.03em",
            lineHeight: 1.04,
            fontVariationSettings: '"opsz" 96',
            maxWidth: "1100px",
          }}
        >
          {strippedTitle(data.title)}
        </h1>

        {version && (
          <p
            className="text-text-muted"
            style={{ fontSize: "15px" }}
          >
            Version {version.version} · Uploaded{" "}
            {relativeAge(version.uploaded_at) ?? "—"}
            {version.uploaded_by && (
              <>
                {" "}
                by <span className="text-text">{version.uploaded_by}</span>
              </>
            )}
          </p>
        )}
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

      <div className="grid grid-cols-1 gap-10 lg:grid-cols-[1fr_320px]">
        <div className="space-y-8">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2
              className="font-display text-text"
              style={{
                fontSize: "24px",
                letterSpacing: "-0.02em",
                fontWeight: 400,
              }}
            >
              Extracted claims
            </h2>
            {version && !editing && (
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="btn-ghost"
                disabled={busy}
              >
                Edit claims
              </button>
            )}
          </div>

          {version ? (
            editing ? (
              <ClaimsEditor
                initial={version.claims}
                onCancel={() => setEditing(false)}
                onSave={onClaimsSave}
                busy={busy}
              />
            ) : (
              <ClaimsDisplay claims={version.claims} />
            )
          ) : (
            <p className="italic text-text-dim" style={{ fontSize: "14px" }}>
              No version uploaded yet.
            </p>
          )}
        </div>

        <aside className="space-y-6">
          <div
            className="rounded-2xl border border-border bg-bg-elevated p-6 backdrop-blur-md"
            style={{ borderRadius: "18px" }}
          >
            <div
              className="text-text-dim mb-4"
              style={{
                fontSize: "11px",
                letterSpacing: "0.14em",
                textTransform: "uppercase",
                fontWeight: 500,
              }}
            >
              File
            </div>
            {version ? (
              <dl className="space-y-3">
                <SidebarRow label="Format">
                  <span className="font-mono text-text" style={{ fontSize: "13px" }}>
                    {version.mime_type}
                  </span>
                </SidebarRow>
                <SidebarRow label="Size">
                  <span className="text-text" style={{ fontSize: "14px" }}>
                    {formatBytes(version.bytes)}
                  </span>
                </SidebarRow>
                <SidebarRow label="SHA-256">
                  <span
                    className="font-mono text-text-muted"
                    style={{ fontSize: "11px" }}
                    title={version.sha256}
                  >
                    {version.sha256.slice(0, 12)}…
                  </span>
                </SidebarRow>
                <SidebarRow label="Storage key">
                  <span
                    className="font-mono text-text-muted"
                    style={{ fontSize: "11px", wordBreak: "break-all" }}
                  >
                    {version.storage_key}
                  </span>
                </SidebarRow>
                <SidebarRow label="Issued">
                  <span className="text-text" style={{ fontSize: "14px" }}>
                    {formatDate(version.issued_date) ?? "—"}
                  </span>
                </SidebarRow>
                <SidebarRow label="Expires">
                  <span className="text-text" style={{ fontSize: "14px" }}>
                    {formatDate(version.expiry_date) ?? "—"}
                  </span>
                </SidebarRow>
                <SidebarRow label="Confirmed">
                  <span
                    className={
                      version.claims_confirmed ? "text-mint-pale" : "text-text-muted"
                    }
                    style={{ fontSize: "14px" }}
                  >
                    {version.claims_confirmed ? "Yes" : "Not yet"}
                  </span>
                </SidebarRow>
              </dl>
            ) : null}
          </div>

          <div className="space-y-3">
            <input
              ref={superSedeRef}
              type="file"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void onSupersede(f);
              }}
            />
            <button
              type="button"
              onClick={() => superSedeRef.current?.click()}
              disabled={busy}
              className="btn-secondary w-full justify-center"
            >
              Upload new version
            </button>
            <button
              type="button"
              onClick={onArchive}
              disabled={busy}
              className="btn-danger w-full justify-center"
            >
              Archive document
            </button>
          </div>
        </aside>
      </div>

      {version?.text_extracted && (
        <details
          className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md"
          style={{ borderRadius: "18px" }}
        >
          <summary
            className="cursor-pointer text-text-muted hover:text-text"
            style={{
              fontSize: "11px",
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              fontWeight: 500,
            }}
          >
            Show extracted text ({version.text_extracted.length.toLocaleString()} chars)
          </summary>
          <pre
            className="mt-5 max-h-[480px] overflow-auto rounded-md border border-border bg-bg p-5 font-mono text-text-muted whitespace-pre-wrap"
            style={{ fontSize: "12px", lineHeight: 1.6 }}
          >
            {version.text_extracted}
          </pre>
        </details>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sidebar row
// ---------------------------------------------------------------------------

function SidebarRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[80px_1fr] items-baseline gap-3">
      <dt
        className="text-text-dim"
        style={{
          fontSize: "10px",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          fontWeight: 500,
        }}
      >
        {label}
      </dt>
      <dd>{children}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Claims editor — for v1, edit the raw JSON. Schema-aware per-discriminator
// editing is a follow-up; what's here gives the user enough to fix wrong
// auto-extracted values without dropping to curl.
// ---------------------------------------------------------------------------

function ClaimsEditor({
  initial,
  onSave,
  onCancel,
  busy,
}: {
  initial: ClaimsRecord;
  onSave: (claims: ClaimsRecord) => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const [raw, setRaw] = useState(() => JSON.stringify(initial, null, 2));
  const [error, setError] = useState<string | null>(null);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    let parsed: ClaimsRecord;
    try {
      parsed = JSON.parse(raw) as ClaimsRecord;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Invalid JSON");
      return;
    }
    setError(null);
    onSave(parsed);
  };
  return (
    <form
      onSubmit={submit}
      className="rounded-2xl border border-mint/30 bg-bg-elevated-strong p-7 space-y-5 backdrop-blur-md"
      style={{ borderRadius: "18px" }}
    >
      <div>
        <h3
          className="font-display text-text mb-2"
          style={{ fontSize: "20px", letterSpacing: "-0.015em" }}
        >
          Edit claims
        </h3>
        <p
          className="text-text-muted"
          style={{ fontSize: "13px", lineHeight: 1.55 }}
        >
          Edit the structured claims as JSON. Keep the{" "}
          <code className="font-mono text-text">doc_type</code> consistent with
          the schema (insurance_certificate / iso_certificate / policy /
          case_study / accounts). Dates are ISO format (YYYY-MM-DD); decimals
          can be strings or numbers.
        </p>
      </div>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        rows={Math.min(24, Math.max(12, raw.split("\n").length + 1))}
        spellCheck={false}
        className="w-full rounded-md border border-border-strong bg-bg p-4 font-mono text-text focus:border-mint focus:outline-none"
        style={{ fontSize: "12px", lineHeight: 1.6 }}
      />
      {error && (
        <p
          role="alert"
          className="text-danger"
          style={{ fontSize: "12px", lineHeight: 1.55 }}
        >
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-3 border-t border-border pt-5">
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? "Saving…" : "Save claims"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="btn-secondary"
          disabled={busy}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function readField(claims: ClaimsRecord, name: string): string | null {
  const v = (claims as Record<string, unknown>)[name];
  if (typeof v !== "string" || !v) return null;
  return v;
}

function strippedTitle(title: string): string {
  if (title.startsWith("PLACEHOLDER -- ")) return title.slice("PLACEHOLDER -- ".length);
  if (title.startsWith("PLACEHOLDER — ")) return title.slice("PLACEHOLDER — ".length);
  return title;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

function DetailSkeleton() {
  return (
    <div className="space-y-10 py-14" aria-busy="true">
      <div className="space-y-5">
        <div className="shimmer rounded-md h-4 w-32" />
        <div className="shimmer rounded-md h-12 w-3/4" />
        <div className="shimmer rounded-md h-5 w-1/3" />
      </div>
      <div className="grid grid-cols-1 gap-10 lg:grid-cols-[1fr_320px]">
        <div className="shimmer rounded-2xl" style={{ height: "420px", borderRadius: "18px" }} />
        <div className="shimmer rounded-2xl" style={{ height: "320px", borderRadius: "18px" }} />
      </div>
    </div>
  );
}

function NotFound() {
  return (
    <div className="py-24 text-center">
      <div
        className="mx-auto max-w-xl rounded-2xl border border-border bg-bg-elevated p-12 backdrop-blur-md"
        style={{ borderRadius: "24px" }}
      >
        <h1 className="font-display text-text mb-4" style={{ fontSize: "32px" }}>
          Document not found.
        </h1>
        <p className="text-text-muted mb-7" style={{ fontSize: "15px", lineHeight: 1.55 }}>
          Nothing exists at this vault ID. It may have been archived already.
        </p>
        <Link href="/vault" className="btn-secondary">
          ← Back to vault
        </Link>
      </div>
    </div>
  );
}

function TopLevelError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="py-14">
      <div
        role="alert"
        className="rounded-2xl border border-danger/40 bg-bg-elevated p-10 backdrop-blur-md"
        style={{ borderRadius: "24px" }}
      >
        <h1 className="font-display text-text mb-3" style={{ fontSize: "24px" }}>
          Couldn&apos;t load this document.
        </h1>
        <button type="button" onClick={onRetry} className="btn-secondary">
          ↻ Retry
        </button>
      </div>
    </div>
  );
}
