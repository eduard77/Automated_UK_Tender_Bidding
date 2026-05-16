"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import useSWR from "swr";

import PillKicker from "./PillKicker";
import StatBlock from "./StatBlock";
import VaultDocumentCard from "./VaultDocumentCard";
import VaultUploadModal from "./VaultUploadModal";
import {
  archiveVaultDocument,
  fetcher,
  isPlaceholderTitle,
  vaultCategoryLabel,
  type VaultCategory,
  type VaultDocument,
} from "@/lib/api";
import { daysUntil, relativeAge } from "@/lib/format";

const CATEGORY_ORDER: VaultCategory[] = [
  "insurance",
  "accreditation",
  "policy",
  "financial",
  "capability",
  "people",
  "technical",
  "past_bid",
  "corporate",
  "uncategorised",
];

export default function VaultManager() {
  return (
    <Suspense fallback={null}>
      <VaultManagerInner />
    </Suspense>
  );
}

function VaultManagerInner() {
  const searchParams = useSearchParams();
  const { data, error, isLoading, mutate } = useSWR<VaultDocument[]>(
    "/vault/documents",
    fetcher,
    { revalidateOnFocus: true },
  );

  const [uploadOpen, setUploadOpen] = useState(false);
  const [topError, setTopError] = useState<string | null>(null);

  // Allow ?upload=1 to auto-open the modal — used for tests, screenshots,
  // and onboarding shortcuts from other pages.
  useEffect(() => {
    if (searchParams.get("upload") === "1") {
      setUploadOpen(true);
    }
  }, [searchParams]);

  const placeholders = useMemo(
    () => (data ?? []).filter((d) => isPlaceholderTitle(d.title)),
    [data],
  );

  const groups = useMemo(() => {
    const by: Record<string, VaultDocument[]> = {};
    for (const d of data ?? []) {
      (by[d.category] ??= []).push(d);
    }
    return CATEGORY_ORDER.filter((c) => by[c]?.length).map((c) => ({
      category: c,
      docs: by[c],
    }));
  }, [data]);

  const stats = useMemo(() => {
    const total = data?.length ?? 0;
    const categories = new Set((data ?? []).map((d) => d.category)).size;
    const expiringSoon = (data ?? []).filter((d) => {
      const exp = d.current_version?.expiry_date;
      if (!exp) return false;
      const days = daysUntil(exp);
      return days !== null && days >= 0 && days <= 30;
    }).length;
    let latest: string | null = null;
    for (const d of data ?? []) {
      const up = d.current_version?.uploaded_at;
      if (up && (!latest || up > latest)) latest = up;
    }
    return { total, categories, expiringSoon, latest };
  }, [data]);

  const archiveDoc = async (doc: VaultDocument) => {
    const ok = window.confirm(
      `Archive "${doc.title}"? Bids referencing this version will keep working; it disappears from the vault list.`,
    );
    if (!ok) return;
    setTopError(null);
    await mutate(
      (cur) => (cur ?? []).filter((d) => d.id !== doc.id),
      { revalidate: false },
    );
    try {
      await archiveVaultDocument(doc.id);
    } catch (err) {
      setTopError(err instanceof Error ? err.message : "Archive failed");
      await mutate();
    }
  };

  return (
    <div className="space-y-12 py-14">
      <header className="space-y-6">
        <PillKicker>Capability vault · your evidence</PillKicker>
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
          What you can prove.
        </h1>
        <p
          className="text-text-muted"
          style={{ fontSize: "17px", lineHeight: 1.6, maxWidth: "720px" }}
        >
          Every certificate, policy, case study and accounts page that
          backs a bid response lives here. The matcher reads the structured
          claims; you read the cards.
        </p>
        <div className="flex flex-wrap items-center gap-4 pt-2">
          <button
            type="button"
            onClick={() => setUploadOpen(true)}
            className="btn-primary"
          >
            + Upload document <span className="arrow">→</span>
          </button>
          <span className="text-text-dim" style={{ fontSize: "13px" }}>
            {data
              ? `${data.length} document${data.length === 1 ? "" : "s"} in vault`
              : isLoading
                ? "Loading…"
                : ""}
          </span>
        </div>
      </header>

      {placeholders.length > 0 && (
        <PlaceholderBanner count={placeholders.length} />
      )}

      {data && data.length > 0 && (
        <div className="flex flex-wrap gap-x-14 gap-y-6 border-y border-border py-8">
          <StatBlock label="Total documents" value={stats.total.toString()} unit="in vault" />
          <StatBlock
            label="Categories"
            value={stats.categories.toString()}
            unit={stats.categories === 1 ? "type" : "types"}
          />
          <StatBlock
            label="Expiring in 30 days"
            value={stats.expiringSoon.toString()}
            unit={stats.expiringSoon === 1 ? "doc" : "docs"}
            tone={stats.expiringSoon > 0 ? "mint" : "default"}
          />
          <StatBlock
            label="Last upload"
            value={stats.latest ? (relativeAge(stats.latest) ?? "—") : "—"}
          />
        </div>
      )}

      {topError && (
        <div
          role="alert"
          className="rounded-xl border border-danger/40 bg-danger/5 p-5 text-text"
          style={{ borderRadius: "18px", fontSize: "14px" }}
        >
          {topError}
        </div>
      )}

      {error ? (
        <ErrorState onRetry={() => mutate()} />
      ) : isLoading && !data ? (
        <SkeletonList />
      ) : data && data.length > 0 ? (
        <div className="space-y-12">
          {groups.map(({ category, docs }) => (
            <section key={category} className="space-y-6">
              <div className="flex items-baseline justify-between gap-4">
                <h2
                  className="font-display text-text"
                  style={{
                    fontSize: "26px",
                    letterSpacing: "-0.02em",
                    fontWeight: 400,
                    fontVariationSettings: '"opsz" 72',
                  }}
                >
                  {vaultCategoryLabel(category)}
                </h2>
                <span
                  className="text-text-dim"
                  style={{
                    fontSize: "11px",
                    letterSpacing: "0.12em",
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  {docs.length} document{docs.length === 1 ? "" : "s"}
                </span>
              </div>
              <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
                {docs.map((d) => (
                  <VaultDocumentCard
                    key={d.id}
                    doc={d}
                    onArchive={() => archiveDoc(d)}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <EmptyState onUpload={() => setUploadOpen(true)} />
      )}

      <VaultUploadModal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={() => {
          void mutate();
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

function PlaceholderBanner({ count }: { count: number }) {
  return (
    <div
      role="alert"
      className="rounded-2xl border p-7 backdrop-blur-md"
      style={{
        borderRadius: "18px",
        background: "rgba(245, 166, 35, 0.08)",
        borderColor: "rgba(245, 166, 35, 0.25)",
      }}
    >
      <div className="flex flex-wrap items-start gap-4">
        <span
          className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-amber/40 text-amber"
          aria-hidden="true"
          style={{ background: "rgba(245, 166, 35, 0.08)" }}
        >
          ⚠
        </span>
        <div className="flex-1 min-w-0">
          <h2
            className="font-display text-text mb-2"
            style={{ fontSize: "20px", letterSpacing: "-0.015em", lineHeight: 1.2 }}
          >
            {count} placeholder document{count === 1 ? "" : "s"} in the vault.
          </h2>
          <p
            className="text-text-muted"
            style={{ fontSize: "14px", lineHeight: 1.55 }}
          >
            These were seeded for testing — every title is prefixed{" "}
            <code className="font-mono text-text">PLACEHOLDER --</code>. Replace
            them with real evidence before relying on bid assessments. Archive
            each card individually once a real version has been uploaded.
          </p>
        </div>
      </div>
    </div>
  );
}

function EmptyState({ onUpload }: { onUpload: () => void }) {
  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-14 text-center backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <svg
        width="120"
        height="120"
        viewBox="0 0 200 160"
        className="mx-auto mb-7 text-text-dim"
        fill="none"
        stroke="currentColor"
        strokeWidth="0.8"
        aria-hidden="true"
      >
        <path d="M30 70 L100 30 L170 70 L100 110 Z" />
        <path d="M30 70 L30 130 L100 170 L100 110" />
        <path d="M170 70 L170 130 L100 170" />
        <path d="M55 55 L125 95 M85 35 L155 75 M55 85 L55 145 M85 65 L85 125 M115 105 L115 165 M145 85 L145 145" />
      </svg>
      <h2
        className="font-display text-text mb-4"
        style={{ fontSize: "28px", letterSpacing: "-0.02em" }}
      >
        No evidence yet.
      </h2>
      <p
        className="text-text-muted mb-7 mx-auto max-w-md"
        style={{ fontSize: "15px", lineHeight: 1.6 }}
      >
        Every bid response starts with what you can prove. Add the
        certificates, policies, and case studies you reach for first.
      </p>
      <button type="button" onClick={onUpload} className="btn-primary">
        + Upload your first document <span className="arrow">→</span>
      </button>
    </div>
  );
}

function SkeletonList() {
  return (
    <div className="space-y-10" aria-busy="true">
      {Array.from({ length: 2 }).map((_, gi) => (
        <div key={gi} className="space-y-6">
          <div className="shimmer rounded-md h-7 w-40" />
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <div
                key={i}
                className="shimmer rounded-2xl"
                style={{ height: "200px", borderRadius: "18px" }}
              />
            ))}
          </div>
        </div>
      ))}
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
        Couldn&apos;t load the vault
      </h2>
      <p className="text-text-muted mt-3" style={{ fontSize: "15px" }}>
        The backend at <code className="font-mono text-text">/vault/documents</code>{" "}
        didn&apos;t respond. Try again, or jump to{" "}
        <Link href="/filters" className="text-mint-pale underline-offset-2 hover:underline">
          filters
        </Link>{" "}
        in the meantime.
      </p>
      <button type="button" onClick={onRetry} className="btn-secondary mt-6">
        ↻ Retry
      </button>
    </div>
  );
}
