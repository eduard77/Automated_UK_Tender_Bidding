"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";

import PillKicker from "./PillKicker";
import { UnlockOverlay } from "./UnlockOverlay";
import {
  ApiError,
  cancelFetch,
  confirmFetch,
  documentFileUrl,
  fetcher,
  getFetchStatus,
  isBriefActive,
  isFetchTaskActive,
  startFetchDocuments,
  startGenerateBrief,
  type BriefDocumentConsidered,
  type BriefJson,
  type BriefKeyRisk,
  type BriefPreviewCounts,
  type BriefScoring,
  type FetchTask,
  type RiskSeverity,
  type Tender,
  type TenderBrief,
  type TenderDocumentFile,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  absoluteDeadline,
  daysUntil,
  formatCurrency,
  formatDate,
  relativeDeadline,
  sourceLabel,
} from "@/lib/format";

export default function TenderDetail({ id }: { id: number }) {
  const { me } = useAuth();
  const tender = useSWR<Tender>(`/tenders/${id}`, fetcher, {
    revalidateOnFocus: false,
  });
  const brief = useSWR<TenderBrief | null>(
    `/tenders/${id}/brief`,
    fetcher,
    { revalidateOnFocus: false, shouldRetryOnError: false },
  );
  const documents = useSWR<TenderDocumentFile[]>(
    `/tenders/${id}/documents`,
    fetcher,
    { revalidateOnFocus: false },
  );

  // When the user signs in or out the backend gating contract changes
  // (locked preview ↔ full brief). Re-run the brief fetch so the page
  // doesn't strand on a stale half-preview after login. Keyed by account
  // id rather than the whole `me` object so we don't churn on identical
  // re-renders.
  const accountId = me?.id ?? null;
  const briefMutate = brief.mutate;
  useEffect(() => {
    void briefMutate();
  }, [accountId, briefMutate]);

  const [fetchTask, setFetchTask] = useState<FetchTask | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const pollTask = useCallback(
    (taskId: string) => {
      const tick = async () => {
        try {
          const task = await getFetchStatus(id, taskId);
          setFetchTask(task);
          if (isFetchTaskActive(task.status)) {
            pollRef.current = setTimeout(tick, 1500);
          } else {
            void documents.mutate();
          }
        } catch {
          setFetchError("Lost track of the fetch task.");
        }
      };
      pollRef.current = setTimeout(tick, 1200);
    },
    [id, documents],
  );

  const startFetch = useCallback(async () => {
    setFetchError(null);
    if (pollRef.current) clearTimeout(pollRef.current);
    try {
      const task = await startFetchDocuments(id);
      setFetchTask(task);
      pollTask(task.task_id);
    } catch {
      setFetchError("Couldn't start the document fetch.");
    }
  }, [id, pollTask]);

  const confirmExpressInterest = useCallback(async () => {
    if (!fetchTask) return;
    setFetchError(null);
    if (pollRef.current) clearTimeout(pollRef.current);
    try {
      const task = await confirmFetch(id, fetchTask.task_id);
      setFetchTask(task);
      pollTask(task.task_id);
    } catch {
      setFetchError("Couldn't confirm — try again.");
    }
  }, [id, fetchTask, pollTask]);

  const cancelTask = useCallback(async () => {
    if (!fetchTask) return;
    if (pollRef.current) clearTimeout(pollRef.current);
    try {
      const task = await cancelFetch(id, fetchTask.task_id);
      setFetchTask(task);
    } catch {
      setFetchError("Couldn't cancel — try again.");
    }
  }, [id, fetchTask]);

  if (tender.error instanceof ApiError && tender.error.status === 404) {
    return <NotFound />;
  }
  if (tender.error) {
    return <TopLevelError onRetry={() => tender.mutate()} />;
  }
  if (!tender.data) {
    return <DetailSkeleton />;
  }

  const hasDocuments = (documents.data ?? []).length > 0;

  return (
    <div className="space-y-14 py-14">
      <Header tender={tender.data} />
      <Description description={tender.data.description} />
      <Sidebar tender={tender.data} />
      <BriefPanel
        tenderId={id}
        brief={brief.data ?? null}
        otherError={brief.error}
        hasDocuments={hasDocuments}
        onReload={() => brief.mutate()}
      />
      <DocumentsSection
        tenderId={id}
        files={documents.data ?? null}
        loading={!documents.data && !documents.error}
        error={documents.error}
        onRetry={() => documents.mutate()}
        onFetch={startFetch}
        onConfirm={confirmExpressInterest}
        onCancel={cancelTask}
        task={fetchTask}
        fetchError={fetchError}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Header / hero
// ---------------------------------------------------------------------------

function Header({ tender }: { tender: Tender }) {
  const deadline = relativeDeadline(tender.deadline_at);
  const days = daysUntil(tender.deadline_at);
  return (
    <header className="space-y-6">
      <Link
        href="/"
        className="inline-flex items-center gap-2 text-[13px] text-text-muted hover:text-text"
      >
        ← Back to today
      </Link>

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
          title={sourceLabel(tender.source_code)}
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
        {deadline.urgent && days !== null && days >= 0 && (
          <PillKicker withDot={false} className="ml-1">
            <span className="text-amber">
              Closes in {days} day{days === 1 ? "" : "s"}
            </span>
          </PillKicker>
        )}
      </div>

      <h1
        className="font-display text-text text-balance"
        style={{
          fontSize: "clamp(36px, 5vw, 64px)",
          fontWeight: 400,
          letterSpacing: "-0.03em",
          lineHeight: 1.04,
          fontVariationSettings: '"opsz" 96',
          maxWidth: "1100px",
        }}
      >
        {tender.title}
      </h1>

      {tender.buyer_name && (
        <p
          className="text-text-muted"
          style={{ fontSize: "18px" }}
        >
          <span className="text-text" style={{ fontWeight: 500 }}>
            {tender.buyer_name}
          </span>
        </p>
      )}
    </header>
  );
}

function Description({ description }: { description: string | null }) {
  if (!description) return null;
  // Split on double newlines into paragraphs for breathing room.
  const paragraphs = description.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  return (
    <section className="max-w-3xl">
      <div className="section-eyebrow mb-6">About this opportunity</div>
      <div className="space-y-5 text-text/90" style={{ fontSize: "16px", lineHeight: 1.7 }}>
        {paragraphs.length > 0 ? (
          paragraphs.map((p, i) => <p key={i}>{p}</p>)
        ) : (
          <p>{description}</p>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Sidebar — value, deadline, dates, cpv, source url, documents teaser
// ---------------------------------------------------------------------------

function Sidebar({ tender }: { tender: Tender }) {
  const value = formatCurrency(tender.value_amount, tender.value_currency);
  const days = daysUntil(tender.deadline_at);
  return (
    <section
      className="rounded-2xl border border-border-strong bg-bg-elevated p-8 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <div className="grid grid-cols-1 gap-x-12 gap-y-8 md:grid-cols-2">
        <DataPoint label="Estimated value">
          <span
            className={`display-num ${value ? "text-mint-pale" : "text-text-dim italic"}`}
            style={{ fontSize: "28px", lineHeight: 1 }}
          >
            {value ?? "Value not stated"}
          </span>
        </DataPoint>
        <DataPoint label="Closes">
          <span
            className={`display-num ${days !== null && days >= 0 && days <= 14 ? "text-amber" : "text-text"}`}
            style={{ fontSize: "28px", lineHeight: 1 }}
          >
            {days !== null && days >= 0
              ? `${days} day${days === 1 ? "" : "s"}`
              : days !== null && days < 0
                ? "Closed"
                : "TBC"}
          </span>
          {tender.deadline_at && (
            <span
              className="block text-text-dim mt-1"
              style={{ fontSize: "13px" }}
            >
              {absoluteDeadline(tender.deadline_at)}
            </span>
          )}
        </DataPoint>
        {tender.published_at && (
          <DataPoint label="Published">
            <span className="text-text" style={{ fontSize: "16px" }}>
              {formatDate(tender.published_at)}
            </span>
          </DataPoint>
        )}
        <DataPoint label="Source">
          <span className="text-text" style={{ fontSize: "16px" }}>
            {sourceLabel(tender.source_code)}
          </span>
        </DataPoint>
      </div>

      {tender.cpv_codes && tender.cpv_codes.length > 0 && (
        <div className="mt-8 border-t border-border pt-6">
          <div
            className="mb-3 text-text-dim"
            style={{
              fontSize: "11px",
              letterSpacing: "0.12em",
              textTransform: "uppercase",
              fontWeight: 500,
            }}
          >
            CPV codes
          </div>
          <div className="flex flex-wrap gap-1.5">
            {tender.cpv_codes.map((c) => (
              <span
                key={c}
                className="font-mono text-text-muted"
                style={{
                  fontSize: "11px",
                  padding: "3px 8px",
                  background: "rgba(255, 255, 255, 0.04)",
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                  borderRadius: "4px",
                  letterSpacing: "0.02em",
                }}
              >
                {c}
              </span>
            ))}
          </div>
        </div>
      )}

      {tender.source_url && (
        <div className="mt-8 flex flex-wrap gap-3">
          <a
            href={tender.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="btn-secondary"
          >
            Open in source portal ↗
          </a>
        </div>
      )}
    </section>
  );
}

function DataPoint({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
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
        {label}
      </span>
      <div>{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Brief panel (chunk 5 — bid / no-bid, key risks first)
// ---------------------------------------------------------------------------

function BriefPanel({
  tenderId,
  brief,
  otherError,
  hasDocuments,
  onReload,
}: {
  tenderId: number;
  brief: TenderBrief | null;
  otherError: unknown;
  hasDocuments: boolean;
  onReload: () => void;
}) {
  const [localBrief, setLocalBrief] = useState<TenderBrief | null>(brief);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Keep local state in sync with SWR if it refetches.
  if (brief && (!localBrief || brief.id !== localBrief.id)) {
    if (
      !localBrief ||
      Date.parse(brief.updated_at) >= Date.parse(localBrief.updated_at)
    ) {
      setLocalBrief(brief);
    }
  }

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const pollBrief = useCallback(() => {
    const tick = async () => {
      try {
        const apiBase =
          process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "/__api";
        const res = await fetch(`${apiBase}/tenders/${tenderId}/brief`, {
          cache: "no-store",
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const next = (await res.json()) as TenderBrief | null;
        setLocalBrief(next);
        if (next && isBriefActive(next.status)) {
          pollRef.current = setTimeout(tick, 2000);
        } else {
          stopPolling();
          onReload();
        }
      } catch {
        stopPolling();
        setError("Lost track of brief generation.");
      }
    };
    pollRef.current = setTimeout(tick, 1500);
  }, [tenderId, onReload, stopPolling]);

  const trigger = useCallback(async () => {
    setError(null);
    setBusy(true);
    stopPolling();
    try {
      const started = await startGenerateBrief(tenderId);
      setLocalBrief(started);
      pollBrief();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start generation");
    } finally {
      setBusy(false);
    }
  }, [tenderId, pollBrief, stopPolling]);

  if (otherError) {
    return (
      <section
        role="alert"
        className="rounded-2xl border border-danger/40 bg-bg-elevated p-8 backdrop-blur-md"
        style={{ borderRadius: "24px" }}
      >
        <h2 className="font-display text-text" style={{ fontSize: "22px" }}>
          Couldn&apos;t load brief
        </h2>
        <button type="button" onClick={onReload} className="btn-secondary mt-5">
          ↻ Retry
        </button>
      </section>
    );
  }

  if (!localBrief) {
    return (
      <BriefCta
        tenderId={tenderId}
        hasDocuments={hasDocuments}
        busy={busy}
        onTrigger={trigger}
        error={error}
      />
    );
  }

  if (localBrief.status === "generating") {
    return <BriefGenerating />;
  }

  if (localBrief.status === "failed") {
    return (
      <BriefFailed
        brief={localBrief}
        hasDocuments={hasDocuments}
        busy={busy}
        onTrigger={trigger}
      />
    );
  }

  return (
    <BriefComplete
      brief={localBrief}
      busy={busy}
      onRegenerate={trigger}
      error={error}
    />
  );
}

function BriefCta({
  tenderId: _tenderId,
  hasDocuments,
  busy,
  onTrigger,
  error,
}: {
  tenderId: number;
  hasDocuments: boolean;
  busy: boolean;
  onTrigger: () => void;
  error: string | null;
}) {
  return (
    <section
      className="featured-glow-line relative overflow-hidden rounded-2xl border border-border-strong bg-bg-elevated p-10 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <PillKicker withDot={false}>Bid brief</PillKicker>
      <h2
        className="font-display text-text mt-5 mb-4"
        style={{ fontSize: "32px", letterSpacing: "-0.02em", lineHeight: 1.1 }}
      >
        No brief generated yet.
      </h2>
      <p
        className="text-text-muted mb-7 max-w-2xl"
        style={{ fontSize: "15px", lineHeight: 1.6 }}
      >
        {hasDocuments
          ? "Read every attached document and produce a recommendation-led brief: BID / NO-BID, confidence, the key risks, deadline, contract value, mandatory requirements, scoring, and what's missing."
          : "Fetch the documents first — the brief reads the ITT pack to recommend BID / NO-BID."}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onTrigger}
          disabled={busy || !hasDocuments}
          className="btn-primary disabled:opacity-50"
        >
          {busy ? "Starting…" : "Generate brief"}
          {!busy && <span className="arrow">→</span>}
        </button>
        {!hasDocuments && (
          <span className="text-text-muted" style={{ fontSize: "13px" }}>
            Scroll down to Fetch documents.
          </span>
        )}
        {error && (
          <span role="alert" className="text-danger" style={{ fontSize: "13px" }}>
            {error}
          </span>
        )}
      </div>
    </section>
  );
}

function BriefGenerating() {
  return (
    <section
      role="status"
      className="rounded-2xl border border-border-strong bg-bg-elevated p-8 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <PillKicker withDot={false}>Bid brief</PillKicker>
      <p
        className="text-text mt-5"
        style={{ fontSize: "16px", lineHeight: 1.6 }}
      >
        <span className="mr-2 inline-block animate-pulse">●</span>
        Reading the documents and analysing…
      </p>
      <p className="text-text-muted mt-3" style={{ fontSize: "13px" }}>
        Usually under a minute. The page updates automatically.
      </p>
    </section>
  );
}

function BriefFailed({
  brief,
  hasDocuments,
  busy,
  onTrigger,
}: {
  brief: TenderBrief;
  hasDocuments: boolean;
  busy: boolean;
  onTrigger: () => void;
}) {
  const noDocs =
    brief.error_detail?.toLowerCase().includes("no documents") === true;
  return (
    <section
      role="alert"
      className="rounded-2xl border border-danger/40 bg-bg-elevated p-8 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <PillKicker withDot={false}>Bid brief</PillKicker>
      <h2
        className="font-display text-text mt-5 mb-3"
        style={{ fontSize: "22px" }}
      >
        {noDocs ? "No documents to analyse" : "Brief generation failed"}
      </h2>
      <p className="text-text-muted mb-6" style={{ fontSize: "14px", lineHeight: 1.6 }}>
        {brief.error_detail ?? "Try again, or check the backend logs."}
      </p>
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          onClick={onTrigger}
          disabled={busy || (!hasDocuments && !noDocs)}
          className="btn-primary disabled:opacity-50"
        >
          {busy ? "Starting…" : noDocs ? "Try again" : "Retry"}
          {!busy && <span className="arrow">→</span>}
        </button>
      </div>
    </section>
  );
}

function BriefComplete({
  brief,
  busy,
  onRegenerate,
  error,
}: {
  brief: TenderBrief;
  busy: boolean;
  onRegenerate: () => void;
  error: string | null;
}) {
  const json = brief.brief_json;
  if (!json) return null;

  // Server-side gating: the backend strips most fields and sets
  // `brief_json.locked = true` for anonymous / unentitled callers. The
  // client never reconstructs the hidden content — it just shows the
  // preview and lets UnlockOverlay drive the funnel.
  if (json.locked) {
    return (
      <LockedBriefPanel
        tenderId={brief.tender_id}
        json={json}
        brief={brief}
        error={error}
      />
    );
  }

  return (
    <section className="space-y-10">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="section-eyebrow">Bid brief</div>
        <button
          type="button"
          onClick={onRegenerate}
          disabled={busy}
          className="btn-ghost"
        >
          {busy ? "Regenerating…" : "Regenerate"}
        </button>
      </div>

      <RecommendationBanner brief={json} />
      <KeyRisks risks={json.key_risks ?? []} />

      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <DeadlineCard deadline={json.deadline} />
        <ContractValueCard value={json.contract_value} />
      </div>

      <MandatoryRequirementsChecklist
        items={json.mandatory_requirements ?? []}
      />

      <ScoringBlock scoring={json.scoring} />

      {json.scope_summary && (
        <BriefSection title="Scope summary">
          <p
            className="max-w-3xl text-text"
            style={{ fontSize: "16px", lineHeight: 1.7 }}
          >
            {json.scope_summary}
          </p>
        </BriefSection>
      )}

      <BulletList
        title="Notable conditions"
        items={json.notable_conditions ?? []}
      />
      <BulletList
        title="Things to clarify with the buyer"
        items={json.missing_or_unclear ?? []}
      />

      <BriefFooter brief={brief} />
      {error && (
        <p role="alert" className="text-danger" style={{ fontSize: "13px" }}>
          {error}
        </p>
      )}
    </section>
  );
}

/**
 * Half-preview brief panel shown when the backend has redacted the brief
 * for an anonymous or unentitled caller (`brief_json.locked === true`).
 *
 * We only render what the server chose to leak (headline, scope_summary)
 * and the counts block ("3 key risks identified"). The UnlockOverlay
 * carries the funnel — it posts to /billing/checkout and redirects.
 */
function LockedBriefPanel({
  tenderId,
  json,
  brief,
  error,
}: {
  tenderId: number;
  json: BriefJson;
  brief: TenderBrief;
  error: string | null;
}) {
  return (
    <section className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="section-eyebrow">Bid brief — preview</div>
        <PillKicker withDot={false}>
          <span className="text-amber">🔒 First half · preview</span>
        </PillKicker>
      </div>

      {json.headline && (
        <div
          className="rounded-2xl border border-amber/30 bg-amber/5 p-7 backdrop-blur-md"
          style={{ borderRadius: "22px" }}
        >
          <div className="section-eyebrow mb-4">Headline</div>
          <p
            className="text-text"
            style={{ fontSize: "18px", lineHeight: 1.55, fontWeight: 500 }}
          >
            {json.headline}
          </p>
        </div>
      )}

      {json.scope_summary && (
        <BriefSection title="Scope summary">
          <p
            className="max-w-3xl text-text"
            style={{ fontSize: "16px", lineHeight: 1.7 }}
          >
            {json.scope_summary}
          </p>
        </BriefSection>
      )}

      {json.counts && <BriefPreviewCountsBlock counts={json.counts} />}

      <UnlockOverlay tenderId={tenderId} surface="brief" />

      <BriefFooter brief={brief} />
      {error && (
        <p role="alert" className="text-danger" style={{ fontSize: "13px" }}>
          {error}
        </p>
      )}
    </section>
  );
}

/**
 * The "3 key risks identified · 6 mandatory requirements · …" strip shown
 * above the unlock overlay. Numbers come straight from the backend's
 * `counts` block; we never compute them client-side.
 */
function BriefPreviewCountsBlock({ counts }: { counts: BriefPreviewCounts }) {
  const items: Array<{ label: string; value: number }> = [];
  if (typeof counts.key_risks_count === "number") {
    items.push({ label: "Key risks identified", value: counts.key_risks_count });
  }
  if (typeof counts.mandatory_requirements_count === "number") {
    items.push({
      label: "Mandatory requirements",
      value: counts.mandatory_requirements_count,
    });
  }
  if (typeof counts.scoring_criteria_count === "number") {
    items.push({
      label: "Scoring criteria",
      value: counts.scoring_criteria_count,
    });
  }
  if (typeof counts.notable_conditions_count === "number") {
    items.push({
      label: "Notable conditions",
      value: counts.notable_conditions_count,
    });
  }
  if (typeof counts.missing_or_unclear_count === "number") {
    items.push({
      label: "Items to clarify",
      value: counts.missing_or_unclear_count,
    });
  }
  if (items.length === 0) return null;
  return (
    <BriefSection title="What's in the full brief">
      <div
        className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
        role="list"
      >
        {items.map((item) => (
          <div
            key={item.label}
            role="listitem"
            className="rounded-xl border border-border bg-bg-elevated p-4 backdrop-blur-sm"
            style={{ borderRadius: "16px" }}
          >
            <div
              className="font-display text-text"
              style={{ fontSize: "28px", fontWeight: 500 }}
            >
              {item.value}
            </div>
            <div
              className="text-text-muted"
              style={{ fontSize: "12px", letterSpacing: "0.06em", textTransform: "uppercase" }}
            >
              {item.label}
            </div>
          </div>
        ))}
      </div>
    </BriefSection>
  );
}

function RecommendationBanner({ brief }: { brief: BriefJson }) {
  // brief_json fields are optional at the wire layer because a locked
  // preview may omit them. In the unlocked render path the server always
  // populates these; guard anyway so a malformed payload can't crash the
  // page.
  if (!brief.recommendation || !brief.confidence) return null;
  const palette =
    brief.recommendation === "bid"
      ? {
          border: "border-mint/40",
          bg: "bg-mint/10",
          text: "text-mint",
          label: "BID",
        }
      : brief.recommendation === "no_bid"
        ? {
            border: "border-danger/40",
            bg: "bg-danger/10",
            text: "text-danger",
            label: "NO-BID",
          }
        : {
            border: "border-amber/40",
            bg: "bg-amber/10",
            text: "text-amber",
            label: "CONDITIONAL",
          };
  return (
    <div
      role="status"
      className={`rounded-2xl border ${palette.border} ${palette.bg} p-7 backdrop-blur-md`}
      style={{ borderRadius: "22px" }}
    >
      <div className="flex flex-wrap items-center gap-4 mb-4">
        <span
          className={`font-display ${palette.text}`}
          style={{
            fontSize: "32px",
            letterSpacing: "0.04em",
            fontWeight: 500,
          }}
        >
          {palette.label}
        </span>
        <ConfidenceChip confidence={brief.confidence} />
      </div>
      {brief.headline && (
        <p
          className="text-text mb-3"
          style={{ fontSize: "18px", lineHeight: 1.55, fontWeight: 500 }}
        >
          {brief.headline}
        </p>
      )}
      {brief.rationale && (
        <p
          className="text-text-muted"
          style={{ fontSize: "15px", lineHeight: 1.65 }}
        >
          {brief.rationale}
        </p>
      )}
    </div>
  );
}

function ConfidenceChip({
  confidence,
}: {
  confidence: "low" | "medium" | "high";
}) {
  const tone =
    confidence === "high"
      ? "border-mint/40 text-mint"
      : confidence === "medium"
        ? "border-amber/40 text-amber"
        : "border-danger/40 text-danger";
  return (
    <span
      className={`shrink-0 whitespace-nowrap rounded-pill border px-2.5 py-1 font-mono ${tone}`}
      style={{
        fontSize: "11px",
        letterSpacing: "0.1em",
        textTransform: "uppercase",
      }}
      aria-label={`Confidence: ${confidence}`}
    >
      {confidence} confidence
    </span>
  );
}

function KeyRisks({ risks }: { risks: BriefKeyRisk[] }) {
  if (!risks?.length) return null;
  return (
    <BriefSection title="Key risks">
      <ol className="space-y-3">
        {risks.map((r, i) => (
          <li
            key={`${i}-${r.risk}`}
            className="rounded-xl border border-border bg-bg-elevated p-5 backdrop-blur-sm"
            style={{ borderRadius: "18px" }}
          >
            <div className="flex items-start justify-between gap-3 mb-2">
              <p
                className="text-text"
                style={{ fontSize: "15px", lineHeight: 1.55, fontWeight: 500 }}
              >
                {r.risk}
              </p>
              <SeverityChip severity={r.severity} />
            </div>
            {r.detail && (
              <p
                className="text-text-muted"
                style={{ fontSize: "13px", lineHeight: 1.6 }}
              >
                {r.detail}
              </p>
            )}
          </li>
        ))}
      </ol>
    </BriefSection>
  );
}

function SeverityChip({ severity }: { severity: RiskSeverity }) {
  const tone =
    severity === "high"
      ? "border-danger/40 text-danger"
      : severity === "medium"
        ? "border-amber/40 text-amber"
        : "border-mint/40 text-mint";
  return (
    <span
      className={`shrink-0 whitespace-nowrap rounded-pill border px-2 py-0.5 font-mono ${tone}`}
      style={{
        fontSize: "10px",
        letterSpacing: "0.1em",
        textTransform: "uppercase",
      }}
      aria-label={`Severity: ${severity}`}
    >
      {severity}
    </span>
  );
}

function DeadlineCard({
  deadline,
}: {
  deadline?: { date?: string | null; note?: string | null } | null;
}) {
  if (!deadline || (!deadline.date && !deadline.note)) return null;
  return (
    <BriefCard label="Deadline">
      {deadline.date && (
        <p className="text-text" style={{ fontSize: "18px", fontWeight: 500 }}>
          {deadline.date}
        </p>
      )}
      {deadline.note && (
        <p
          className="text-text-muted mt-2"
          style={{ fontSize: "13px", lineHeight: 1.55 }}
        >
          {deadline.note}
        </p>
      )}
    </BriefCard>
  );
}

function ContractValueCard({
  value,
}: {
  value?: { amount?: string | null; note?: string | null } | null;
}) {
  if (!value || (!value.amount && !value.note)) return null;
  return (
    <BriefCard label="Contract value">
      {value.amount && (
        <p
          className="display-num text-mint-pale"
          style={{ fontSize: "24px", lineHeight: 1.2 }}
        >
          {value.amount}
        </p>
      )}
      {value.note && (
        <p
          className="text-text-muted mt-2"
          style={{ fontSize: "13px", lineHeight: 1.55 }}
        >
          {value.note}
        </p>
      )}
    </BriefCard>
  );
}

function BriefCard({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="rounded-xl border border-border bg-bg-elevated p-6 backdrop-blur-sm"
      style={{ borderRadius: "18px" }}
    >
      <div
        className="text-text-dim mb-3"
        style={{
          fontSize: "11px",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          fontWeight: 500,
        }}
      >
        {label}
      </div>
      {children}
    </div>
  );
}

function MandatoryRequirementsChecklist({ items }: { items: string[] }) {
  if (!items?.length) return null;
  return (
    <BriefSection title="Mandatory requirements">
      <ul className="space-y-2.5">
        {items.map((req, i) => (
          <li
            key={`${i}-${req}`}
            className="flex items-start gap-3 text-text"
            style={{ fontSize: "15px", lineHeight: 1.55 }}
          >
            <span
              className="mt-1 inline-block shrink-0 rounded-sm border border-mint/40 text-mint"
              style={{
                width: "14px",
                height: "14px",
                fontSize: "10px",
                lineHeight: "12px",
                textAlign: "center",
              }}
              aria-hidden="true"
            >
              ✓
            </span>
            <span>{req}</span>
          </li>
        ))}
      </ul>
    </BriefSection>
  );
}

function ScoringBlock({ scoring }: { scoring?: BriefScoring | null }) {
  if (!scoring) return null;
  const hasContent =
    (scoring.summary && scoring.summary.length > 0) ||
    (scoring.criteria && scoring.criteria.length > 0);
  if (!hasContent) return null;
  return (
    <BriefSection title="Scoring">
      {scoring.summary && (
        <p
          className="text-text mb-4"
          style={{ fontSize: "15px", lineHeight: 1.6 }}
        >
          {scoring.summary}
        </p>
      )}
      {scoring.criteria && scoring.criteria.length > 0 && (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {scoring.criteria.map((c, i) => (
            <div
              key={`${i}-${c.criterion}`}
              className="rounded-xl border border-border bg-bg-elevated p-4 backdrop-blur-sm"
              style={{ borderRadius: "14px" }}
            >
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-text" style={{ fontWeight: 500 }}>
                  {c.criterion}
                </span>
                {c.weight && (
                  <span
                    className="display-num text-mint-pale"
                    style={{ fontSize: "18px" }}
                  >
                    {c.weight}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </BriefSection>
  );
}

function BulletList({ title, items }: { title: string; items: string[] }) {
  if (!items?.length) return null;
  return (
    <BriefSection title={title}>
      <ul className="space-y-2 text-text" style={{ fontSize: "15px", lineHeight: 1.55 }}>
        {items.map((it, i) => (
          <li key={`${i}-${it}`} className="flex gap-3">
            <span className="text-text-dim" aria-hidden="true">·</span>
            <span>{it}</span>
          </li>
        ))}
      </ul>
    </BriefSection>
  );
}

function BriefSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-4">
      <h3
        className="font-display text-text"
        style={{ fontSize: "22px", letterSpacing: "-0.02em" }}
      >
        {title}
      </h3>
      {children}
    </section>
  );
}

function BriefFooter({ brief }: { brief: TenderBrief }) {
  const docs = brief.documents_considered ?? [];
  const included = docs.filter(
    (d: BriefDocumentConsidered) => d.included !== "omitted",
  ).length;
  const truncated = docs.filter(
    (d: BriefDocumentConsidered) => d.included === "truncated",
  ).length;
  const total = docs.length;
  return (
    <footer
      className="flex flex-wrap gap-6 border-t border-border pt-6 text-text-dim"
      style={{
        fontSize: "11px",
        letterSpacing: "0.1em",
        textTransform: "uppercase",
      }}
    >
      {total > 0 && (
        <span>
          Based on {included} of {total} documents
          {truncated > 0 ? ` (${truncated} truncated)` : ""}
        </span>
      )}
      {brief.model && <span>Model: {brief.model}</span>}
      {brief.generated_at && (
        <span>Generated: {formatDate(brief.generated_at)}</span>
      )}
    </footer>
  );
}

// ---------------------------------------------------------------------------
// Documents (downloaded files)
// ---------------------------------------------------------------------------

function DocumentsSection({
  tenderId,
  files,
  loading,
  error,
  onRetry,
  onFetch,
  onConfirm,
  onCancel,
  task,
  fetchError,
}: {
  tenderId: number;
  files: TenderDocumentFile[] | null;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  onFetch: () => void;
  onConfirm: () => void;
  onCancel: () => void;
  task: FetchTask | null;
  fetchError: string | null;
}) {
  const active = task != null && isFetchTaskActive(task.status);
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="section-eyebrow">Attached documents</div>
        <button
          type="button"
          onClick={onFetch}
          disabled={active}
          className="btn-primary disabled:opacity-50"
        >
          {active ? "Fetching…" : "Fetch documents"}{" "}
          <span className="arrow">→</span>
        </button>
      </div>

      {fetchError && (
        <p className="text-danger" style={{ fontSize: "13px" }}>
          {fetchError}
        </p>
      )}
      {task && (
        <FetchStatusBanner
          task={task}
          onConfirm={onConfirm}
          onCancel={onCancel}
          onRetry={onFetch}
        />
      )}

      {error ? (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-danger/40 bg-bg-elevated p-5"
          style={{ borderRadius: "18px" }}
        >
          <span className="text-text" style={{ fontSize: "14px" }}>
            Couldn&apos;t load documents.
          </span>
          <button type="button" onClick={onRetry} className="btn-ghost">
            ↻ Retry
          </button>
        </div>
      ) : loading ? (
        <ul className="space-y-2.5" aria-busy="true">
          {Array.from({ length: 3 }).map((_, i) => (
            <li
              key={i}
              className="shimmer rounded-xl"
              style={{ height: "64px", borderRadius: "18px" }}
            />
          ))}
        </ul>
      ) : !files?.length ? (
        <p className="text-text-dim italic" style={{ fontSize: "14px" }}>
          No documents downloaded yet. Click{" "}
          <span className="text-text-muted">Fetch documents</span> to pull any
          publicly available files.
        </p>
      ) : (
        <ul className="space-y-2.5">
          {files.map((f) => (
            <li key={f.id}>
              <DocumentRow tenderId={tenderId} file={f} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const AMBER = { border: "rgba(245,166,35,0.35)", bg: "rgba(245,166,35,0.07)" };
const GREEN = { border: "rgba(151,213,188,0.3)", bg: "rgba(151,213,188,0.06)" };
const RED = { border: "rgba(248,113,113,0.35)", bg: "rgba(248,113,113,0.07)" };
const NEUTRAL = { border: "rgba(255,255,255,0.12)", bg: "rgba(255,255,255,0.03)" };

function FetchStatusBanner({
  task,
  onConfirm,
  onCancel,
  onRetry,
}: {
  task: FetchTask;
  onConfirm: () => void;
  onCancel: () => void;
  onRetry: () => void;
}) {
  const active = isFetchTaskActive(task.status);
  const platform = task.platform_slug ?? "the portal";

  // --- interpretive / login states get their own action UI ---
  if (task.status === "waiting_for_login") {
    return (
      <BannerShell tone={AMBER}>
        <p className="text-text" style={{ fontSize: "13px" }}>
          <span className="mr-2 inline-block animate-pulse">●</span>
          Waiting for you to log in to <strong>{platform}</strong>. A browser
          window should be open on your PC — log in there and this continues
          automatically.
        </p>
        <BannerActions>
          <button type="button" onClick={onRetry} className="btn-ghost">
            I&apos;ve logged in / Retry
          </button>
          <button type="button" onClick={onCancel} className="btn-ghost">
            Cancel
          </button>
        </BannerActions>
      </BannerShell>
    );
  }

  if (task.status === "needs_user_confirmation") {
    return (
      <BannerShell tone={AMBER}>
        <p className="text-text" style={{ fontSize: "13px" }}>
          {task.detail ??
            `${platform} requires you to Express Interest before documents are released.`}{" "}
          This signals to the buyer that you intend to bid.
        </p>
        <BannerActions>
          <button type="button" onClick={onConfirm} className="btn-primary">
            Confirm &amp; continue <span className="arrow">→</span>
          </button>
          <button type="button" onClick={onCancel} className="btn-ghost">
            Not now
          </button>
        </BannerActions>
      </BannerShell>
    );
  }

  if (task.status === "bridge_unavailable") {
    return (
      <BannerShell tone={RED}>
        <p className="text-text" style={{ fontSize: "13px" }}>
          Browser bridge isn&apos;t running. Open <code>start-bridge.ps1</code> on
          your PC, then retry.
        </p>
        <BannerActions>
          <button type="button" onClick={onRetry} className="btn-ghost">
            ↻ Retry
          </button>
        </BannerActions>
      </BannerShell>
    );
  }

  // --- plain status messages ---
  let tone = NEUTRAL;
  let message: string;
  if (active) {
    tone = NEUTRAL;
    message = "Fetching documents from the source…";
  } else if (task.status === "complete" && task.files_count === 0) {
    tone = GREEN;
    message = `No documents available for this tender${
      task.adapter === "ContractsFinderDirectAdapter" ? " from CF" : ""
    }.`;
  } else if (task.status === "complete") {
    tone = GREEN;
    message = `Fetched ${task.files_count} document${task.files_count === 1 ? "" : "s"}.`;
  } else if (task.status === "partial") {
    tone = AMBER;
    message = `Limited documents available — ${task.files_count} fetched, ${task.missing_count} require login${
      task.platform_slug ? ` to ${task.platform_slug}` : ""
    }.`;
  } else if (task.status === "no_portal") {
    tone = NEUTRAL;
    message = "No known portal hosts this tender's documents.";
  } else if (task.status === "failed" || task.status === "error") {
    tone = RED;
    message = `Fetch failed${task.detail ? `: ${task.detail}` : ""}.`;
  } else {
    message = `Status: ${task.status}.`;
  }

  return (
    <BannerShell tone={tone}>
      <span className="text-text" style={{ fontSize: "13px" }}>
        {active && <span className="mr-2 inline-block animate-pulse">●</span>}
        {message}
      </span>
    </BannerShell>
  );
}

function BannerShell({
  tone,
  children,
}: {
  tone: { border: string; bg: string };
  children: React.ReactNode;
}) {
  return (
    <div
      role="status"
      className="rounded-xl border p-4 space-y-3"
      style={{ borderRadius: "14px", border: `1px solid ${tone.border}`, background: tone.bg }}
    >
      {children}
    </div>
  );
}

function BannerActions({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap items-center gap-3">{children}</div>;
}

function DocumentRow({ tenderId, file }: { tenderId: number; file: TenderDocumentFile }) {
  const downloadable = file.download_status === "ok";
  const href = downloadable ? documentFileUrl(tenderId, file.id) : file.url;
  return (
    <div
      className="flex items-center gap-4 rounded-xl border border-border bg-bg-elevated p-5 backdrop-blur-sm"
      style={{ borderRadius: "18px" }}
    >
      <DownloadStatusBadge status={file.download_status} />
      <div className="flex-1 min-w-0">
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="block truncate text-text hover:text-mint-pale focus:outline-none"
          style={{ fontSize: "14px" }}
          aria-label={`${downloadable ? "Download" : "Open"} ${file.title ?? "document"}`}
        >
          {file.title || file.url}
        </a>
        {file.error && (
          <p className="text-danger truncate mt-1" style={{ fontSize: "12px" }}>
            {file.error}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2 whitespace-nowrap">
        {file.format && (
          <span
            className="font-mono text-text-muted"
            style={{
              fontSize: "10px",
              padding: "3px 8px",
              border: "1px solid rgba(255, 255, 255, 0.08)",
              borderRadius: "4px",
              letterSpacing: "0.08em",
              textTransform: "uppercase",
            }}
          >
            {file.format}
          </span>
        )}
        {file.bytes != null && (
          <span
            className="font-mono text-text-dim"
            style={{ fontSize: "11px" }}
          >
            {formatBytes(file.bytes)}
          </span>
        )}
        {file.sha256 && (
          <span
            className="font-mono text-text-dim"
            style={{ fontSize: "11px" }}
            title={file.sha256}
            aria-label={`SHA256 ${file.sha256}`}
          >
            {file.sha256.slice(0, 8)}
          </span>
        )}
        {file.content_stored && (
          <span
            className="font-mono text-mint-pale"
            title="Extracted text content is stored — reusable without re-downloading the file"
            aria-label="Content stored"
            style={{
              fontSize: "10px",
              padding: "3px 7px",
              border: "1px solid rgba(151, 213, 188, 0.35)",
              borderRadius: "4px",
              letterSpacing: "0.08em",
              textTransform: "uppercase",
            }}
          >
            ✓ Content
          </span>
        )}
      </div>
    </div>
  );
}

function DownloadStatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase();
  const tone =
    s === "ok" || s === "success"
      ? "border-mint/40 text-mint"
      : s === "error" || s === "failed"
        ? "border-danger/40 text-danger"
        : "border-border-strong text-text-muted";
  return (
    <span
      className={`shrink-0 whitespace-nowrap rounded-md border px-2 py-0.5 font-mono ${tone}`}
      style={{ fontSize: "10px", letterSpacing: "0.1em", textTransform: "uppercase" }}
      aria-label={`Download status: ${status}`}
    >
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

function DetailSkeleton() {
  return (
    <div className="space-y-10 py-14" aria-busy="true" aria-label="Loading tender">
      <div className="space-y-5">
        <div className="shimmer rounded-md h-4 w-32" />
        <div className="shimmer rounded-md h-12 w-3/4" />
        <div className="shimmer rounded-md h-5 w-1/3" />
      </div>
      <div className="shimmer rounded-2xl" style={{ height: "180px", borderRadius: "24px" }} />
      <div className="shimmer rounded-md h-5 w-1/3" />
      <div className="space-y-3">
        <div className="shimmer rounded-md h-4 w-full" />
        <div className="shimmer rounded-md h-4 w-5/6" />
        <div className="shimmer rounded-md h-4 w-4/6" />
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
          Tender not found.
        </h1>
        <p className="text-text-muted mb-7" style={{ fontSize: "15px", lineHeight: 1.55 }}>
          Nothing exists at this ID. It may have been deduplicated into another
          record, or never imported.
        </p>
        <Link href="/" className="btn-secondary">
          ← Back to today
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
          Couldn&apos;t load this tender.
        </h1>
        <p className="text-text-muted mb-6" style={{ fontSize: "15px" }}>
          The backend didn&apos;t respond. Retry?
        </p>
        <button type="button" onClick={onRetry} className="btn-secondary">
          ↻ Retry
        </button>
      </div>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}
