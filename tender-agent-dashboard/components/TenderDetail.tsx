"use client";

import Link from "next/link";
import { useCallback, useRef, useState } from "react";
import useSWR from "swr";

import PillKicker from "./PillKicker";
import {
  ApiError,
  cancelFetch,
  confirmFetch,
  documentFileUrl,
  fetcher,
  generateTenderBrief,
  getFetchStatus,
  isBriefActive,
  isFetchTaskActive,
  startFetchDocuments,
  type BriefKeyRisk,
  type BriefPayload,
  type BriefRecommendation,
  type BriefSeverity,
  type FetchTask,
  type Tender,
  type TenderBrief,
  type TenderDocumentFile,
} from "@/lib/api";
import {
  absoluteDeadline,
  daysUntil,
  formatCurrency,
  formatDate,
  relativeDeadline,
  sourceLabel,
} from "@/lib/format";

export default function TenderDetail({ id }: { id: number }) {
  const tender = useSWR<Tender>(`/tenders/${id}`, fetcher, {
    revalidateOnFocus: false,
  });
  const brief = useSWR<TenderBrief>(
    `/tenders/${id}/brief`,
    fetcher,
    {
      revalidateOnFocus: false,
      // Poll while a generation is in progress; stop once the latest brief
      // is complete/failed/no_documents.
      refreshInterval: (data) =>
        data && isBriefActive(data.status) ? 1500 : 0,
      shouldRetryOnError: (err) =>
        !(err instanceof ApiError && err.status === 404),
    },
  );
  const documents = useSWR<TenderDocumentFile[]>(
    `/tenders/${id}/documents`,
    fetcher,
    { revalidateOnFocus: false },
  );

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

  const briefData = brief.data ?? null;
  const briefIs404 =
    brief.error instanceof ApiError && brief.error.status === 404;
  const hasAnyDocument = (documents.data?.length ?? 0) > 0;

  return (
    <div className="space-y-14 py-14">
      <Header tender={tender.data} />
      <Description description={tender.data.description} />
      <Sidebar tender={tender.data} />
      <BidBriefBlock
        tenderId={id}
        brief={briefData}
        is404={briefIs404}
        hasDocuments={hasAnyDocument}
        otherError={!briefData && !briefIs404 && brief.error ? brief.error : null}
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
// Bid brief block (Phase 4 chunk 5) — recommendation-led, risks-first.
//
// The brief LEADS with bid / no-bid / conditional + key risks because that's
// what the human reads first when triaging "should we bid?". Detail follows.
// ---------------------------------------------------------------------------

function BidBriefBlock({
  tenderId,
  brief,
  is404,
  hasDocuments,
  otherError,
  onReload,
}: {
  tenderId: number;
  brief: TenderBrief | null;
  is404: boolean;
  hasDocuments: boolean;
  otherError: unknown;
  onReload: () => void;
}) {
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

  if (is404 || !brief) {
    return (
      <GenerateBriefCta
        tenderId={tenderId}
        hasDocuments={hasDocuments}
        onTriggered={onReload}
      />
    );
  }

  if (brief.status === "generating") {
    return <BriefGeneratingBanner />;
  }

  if (brief.status === "no_documents") {
    return (
      <GenerateBriefCta
        tenderId={tenderId}
        hasDocuments={false}
        onTriggered={onReload}
        priorError={brief.error_detail}
      />
    );
  }

  if (brief.status === "failed") {
    return (
      <BriefFailedBanner
        tenderId={tenderId}
        detail={brief.error_detail}
        onRegenerated={onReload}
      />
    );
  }

  const payload = brief.brief_json;
  if (!payload) {
    return (
      <BriefFailedBanner
        tenderId={tenderId}
        detail="Brief was marked complete but the payload is missing."
        onRegenerated={onReload}
      />
    );
  }

  const considered = brief.documents_considered ?? [];
  const totalDocs = considered.length;
  const includedDocs = considered.filter((d) => d.included !== "omitted").length;
  const truncatedDocs = considered.filter((d) => d.included === "truncated").length;

  return (
    <section className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="section-eyebrow">Bid brief</div>
        <RegenerateBriefButton tenderId={tenderId} onTriggered={onReload} />
      </div>

      <RecommendationBanner
        recommendation={payload.recommendation}
        confidence={payload.confidence}
        headline={payload.headline}
        rationale={payload.rationale}
      />

      <KeyRisksList risks={payload.key_risks} />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        <BriefFact
          label="Deadline"
          value={payload.deadline?.date}
          note={payload.deadline?.note}
        />
        <BriefFact
          label="Contract value"
          value={payload.contract_value?.amount}
          note={payload.contract_value?.note}
        />
      </div>

      {payload.mandatory_requirements.length > 0 && (
        <BriefSection title="Mandatory requirements">
          <ul className="space-y-2">
            {payload.mandatory_requirements.map((req, i) => (
              <li
                key={i}
                className="flex items-start gap-3 text-text"
                style={{ fontSize: "14px", lineHeight: 1.55 }}
              >
                <span
                  className="text-mint-pale shrink-0 mt-1"
                  aria-hidden="true"
                  style={{ fontSize: "11px" }}
                >
                  ☐
                </span>
                <span>{req}</span>
              </li>
            ))}
          </ul>
        </BriefSection>
      )}

      {payload.scoring && (payload.scoring.summary || payload.scoring.criteria.length > 0) && (
        <BriefSection title="Scoring">
          {payload.scoring.summary && (
            <p
              className="text-text mb-4"
              style={{ fontSize: "14px", lineHeight: 1.6 }}
            >
              {payload.scoring.summary}
            </p>
          )}
          {payload.scoring.criteria.length > 0 && (
            <ul className="space-y-2">
              {payload.scoring.criteria.map((c, i) => (
                <li
                  key={i}
                  className="flex items-baseline justify-between gap-3 border-b border-border pb-2"
                >
                  <span className="text-text" style={{ fontSize: "14px" }}>
                    {c.criterion}
                  </span>
                  {c.weight && (
                    <span
                      className="display-num text-mint-pale"
                      style={{ fontSize: "16px" }}
                    >
                      {c.weight}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </BriefSection>
      )}

      {payload.scope_summary && (
        <BriefSection title="Scope">
          <p
            className="text-text max-w-3xl"
            style={{ fontSize: "15px", lineHeight: 1.65 }}
          >
            {payload.scope_summary}
          </p>
        </BriefSection>
      )}

      {payload.notable_conditions.length > 0 && (
        <BriefSection title="Notable conditions">
          <ul className="space-y-2">
            {payload.notable_conditions.map((c, i) => (
              <li
                key={i}
                className="text-text"
                style={{ fontSize: "14px", lineHeight: 1.55 }}
              >
                · {c}
              </li>
            ))}
          </ul>
        </BriefSection>
      )}

      {payload.missing_or_unclear.length > 0 && (
        <BriefSection title="Clarify with the buyer">
          <ul className="space-y-2">
            {payload.missing_or_unclear.map((m, i) => (
              <li
                key={i}
                className="text-text"
                style={{ fontSize: "14px", lineHeight: 1.55 }}
              >
                ? {m}
              </li>
            ))}
          </ul>
        </BriefSection>
      )}

      <footer
        className="flex flex-wrap gap-x-6 gap-y-2 border-t border-border pt-6 text-text-dim"
        style={{
          fontSize: "11px",
          letterSpacing: "0.1em",
          textTransform: "uppercase",
        }}
      >
        <span>
          Based on {includedDocs} of {totalDocs} document
          {totalDocs === 1 ? "" : "s"}
          {truncatedDocs > 0
            ? ` (${truncatedDocs} truncated)`
            : ""}
        </span>
        {brief.model && <span>· model: {brief.model}</span>}
        {brief.generated_at && (
          <span>· generated {formatDate(brief.generated_at)}</span>
        )}
      </footer>
    </section>
  );
}

function GenerateBriefCta({
  tenderId,
  hasDocuments,
  onTriggered,
  priorError,
}: {
  tenderId: number;
  hasDocuments: boolean;
  onTriggered: () => void;
  priorError?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(priorError ?? null);
  const trigger = async () => {
    setBusy(true);
    setError(null);
    try {
      await generateTenderBrief(tenderId);
      onTriggered();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start brief");
    } finally {
      setBusy(false);
    }
  };
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
        {hasDocuments ? "No brief generated yet." : "Fetch documents first."}
      </h2>
      <p
        className="text-text-muted mb-7 max-w-2xl"
        style={{ fontSize: "15px", lineHeight: 1.6 }}
      >
        {hasDocuments
          ? "Run Claude over the attached documents to produce a 2-page bid brief — bid / no-bid recommendation, key risks, mandatory requirements, scoring split, and deadline."
          : "The bid brief reads the ITT pack. Fetch the tender's documents below first, then come back here to generate the brief."}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={trigger}
          disabled={busy || !hasDocuments}
          className="btn-primary disabled:opacity-50"
        >
          {busy ? "Starting…" : "Generate brief"}
          {!busy && <span className="arrow">→</span>}
        </button>
        {error && (
          <span role="alert" className="text-danger" style={{ fontSize: "13px" }}>
            {error}
          </span>
        )}
      </div>
    </section>
  );
}

function RegenerateBriefButton({
  tenderId,
  onTriggered,
}: {
  tenderId: number;
  onTriggered: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const trigger = async () => {
    setBusy(true);
    try {
      await generateTenderBrief(tenderId);
      onTriggered();
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      onClick={trigger}
      disabled={busy}
      className="btn-ghost disabled:opacity-50"
    >
      {busy ? "Restarting…" : "↻ Regenerate"}
    </button>
  );
}

function BriefGeneratingBanner() {
  return (
    <section
      className="rounded-2xl border border-border-strong bg-bg-elevated p-8 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <div className="section-eyebrow mb-4">Bid brief</div>
      <p className="text-text" style={{ fontSize: "15px" }}>
        <span className="mr-2 inline-block animate-pulse">●</span>
        Reading the documents and analysing…
      </p>
      <p className="text-text-muted mt-2" style={{ fontSize: "13px" }}>
        Usually takes 30-60 seconds depending on pack size.
      </p>
    </section>
  );
}

function BriefFailedBanner({
  tenderId,
  detail,
  onRegenerated,
}: {
  tenderId: number;
  detail: string | null;
  onRegenerated: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const retry = async () => {
    setBusy(true);
    try {
      await generateTenderBrief(tenderId);
      onRegenerated();
    } finally {
      setBusy(false);
    }
  };
  return (
    <section
      role="alert"
      className="rounded-2xl border border-danger/40 bg-bg-elevated p-8 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <div className="section-eyebrow mb-3">Bid brief</div>
      <h2
        className="font-display text-text mb-3"
        style={{ fontSize: "24px", letterSpacing: "-0.02em" }}
      >
        Brief generation failed
      </h2>
      {detail && (
        <p className="text-text-muted mb-5" style={{ fontSize: "14px" }}>
          {detail}
        </p>
      )}
      <button type="button" onClick={retry} disabled={busy} className="btn-secondary">
        {busy ? "Retrying…" : "↻ Retry"}
      </button>
    </section>
  );
}

function recommendationPalette(rec: BriefRecommendation) {
  if (rec === "bid") {
    return {
      border: "border-mint/40",
      bg: "bg-mint/10",
      text: "text-mint",
      label: "BID",
    };
  }
  if (rec === "no_bid") {
    return {
      border: "border-danger/40",
      bg: "bg-danger/10",
      text: "text-danger",
      label: "NO BID",
    };
  }
  return {
    border: "border-amber/40",
    bg: "bg-amber/10",
    text: "text-amber",
    label: "CONDITIONAL",
  };
}

function RecommendationBanner({
  recommendation,
  confidence,
  headline,
  rationale,
}: {
  recommendation: BriefRecommendation;
  confidence: "high" | "medium" | "low";
  headline: string;
  rationale: string;
}) {
  const palette = recommendationPalette(recommendation);
  return (
    <div
      role="status"
      className={`rounded-2xl border ${palette.border} ${palette.bg} p-7 backdrop-blur-md`}
      style={{ borderRadius: "20px" }}
    >
      <div className="flex flex-wrap items-baseline gap-4 mb-4">
        <span
          className={`font-display ${palette.text}`}
          style={{
            fontSize: "40px",
            fontWeight: 500,
            letterSpacing: "-0.02em",
            lineHeight: 1,
          }}
        >
          {palette.label}
        </span>
        <span
          className="font-mono text-text-muted"
          style={{
            fontSize: "11px",
            letterSpacing: "0.14em",
            textTransform: "uppercase",
          }}
        >
          confidence · {confidence}
        </span>
      </div>
      <h2
        className="font-display text-text mb-3"
        style={{ fontSize: "22px", letterSpacing: "-0.02em", lineHeight: 1.25 }}
      >
        {headline}
      </h2>
      <p className="text-text/90" style={{ fontSize: "15px", lineHeight: 1.65 }}>
        {rationale}
      </p>
    </div>
  );
}

function severityPalette(s: BriefSeverity) {
  if (s === "high") return { border: "border-danger/40", text: "text-danger" };
  if (s === "medium") return { border: "border-amber/40", text: "text-amber" };
  return { border: "border-border-strong", text: "text-text-muted" };
}

function SeverityChip({ severity }: { severity: BriefSeverity }) {
  const p = severityPalette(severity);
  return (
    <span
      className={`shrink-0 whitespace-nowrap rounded border px-2 py-0.5 font-mono ${p.border} ${p.text}`}
      style={{
        fontSize: "10px",
        letterSpacing: "0.12em",
        textTransform: "uppercase",
      }}
      aria-label={`Severity: ${severity}`}
    >
      {severity}
    </span>
  );
}

function KeyRisksList({ risks }: { risks: BriefKeyRisk[] }) {
  if (!risks.length) {
    return (
      <section className="space-y-3">
        <h3
          className="font-display text-text"
          style={{ fontSize: "22px", letterSpacing: "-0.02em" }}
        >
          Key risks
        </h3>
        <p className="text-text-muted italic" style={{ fontSize: "14px" }}>
          No material risks called out.
        </p>
      </section>
    );
  }
  return (
    <section className="space-y-4">
      <h3
        className="font-display text-text"
        style={{ fontSize: "22px", letterSpacing: "-0.02em" }}
      >
        Key risks
      </h3>
      <ul className="space-y-3">
        {risks.map((r, i) => (
          <li
            key={i}
            className="rounded-xl border border-border bg-bg-elevated p-5 backdrop-blur-sm"
            style={{ borderRadius: "16px" }}
          >
            <div className="flex items-start justify-between gap-3 mb-1">
              <p className="text-text" style={{ fontSize: "15px", fontWeight: 500 }}>
                {r.risk}
              </p>
              <SeverityChip severity={r.severity} />
            </div>
            {r.detail && (
              <p
                className="text-text-muted mt-2"
                style={{ fontSize: "13px", lineHeight: 1.55 }}
              >
                {r.detail}
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function BriefFact({
  label,
  value,
  note,
}: {
  label: string;
  value: string | null | undefined;
  note: string | null | undefined;
}) {
  return (
    <div
      className="rounded-xl border border-border bg-bg-elevated p-5"
      style={{ borderRadius: "16px" }}
    >
      <div
        className="text-text-dim mb-2"
        style={{
          fontSize: "11px",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          fontWeight: 500,
        }}
      >
        {label}
      </div>
      <div
        className={`display-num ${value ? "text-text" : "text-text-dim italic"}`}
        style={{ fontSize: "22px", lineHeight: 1.2 }}
      >
        {value || "(not stated)"}
      </div>
      {note && (
        <p className="text-text-muted mt-2" style={{ fontSize: "12px" }}>
          {note}
        </p>
      )}
    </div>
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
    <section className="space-y-3">
      <h3
        className="font-display text-text"
        style={{ fontSize: "20px", letterSpacing: "-0.02em" }}
      >
        {title}
      </h3>
      {children}
    </section>
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
            className="font-mono text-mint"
            style={{
              fontSize: "10px",
              padding: "3px 8px",
              border: "1px solid rgba(151,213,188,0.35)",
              borderRadius: "4px",
              letterSpacing: "0.1em",
              textTransform: "uppercase",
            }}
            title="Content extracted and stored — reusable without re-downloading"
            aria-label="Content stored"
          >
            content stored
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
