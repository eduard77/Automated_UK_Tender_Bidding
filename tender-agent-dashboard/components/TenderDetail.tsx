"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";

import PillKicker from "./PillKicker";
import {
  ApiError,
  fetcher,
  type DocumentRequired,
  type EvaluationCriterion,
  type QuestionToAnswer,
  type RequirementItem,
  type Tender,
  type TenderDocumentFile,
  type TenderRequirements,
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
  const requirements = useSWR<TenderRequirements>(
    `/tenders/${id}/requirements`,
    fetcher,
    {
      revalidateOnFocus: false,
      shouldRetryOnError: (err) =>
        !(err instanceof ApiError && err.status === 404),
    },
  );
  const documents = useSWR<TenderDocumentFile[]>(
    `/tenders/${id}/documents`,
    fetcher,
    { revalidateOnFocus: false },
  );

  if (tender.error instanceof ApiError && tender.error.status === 404) {
    return <NotFound />;
  }
  if (tender.error) {
    return <TopLevelError onRetry={() => tender.mutate()} />;
  }
  if (!tender.data) {
    return <DetailSkeleton />;
  }

  const brief = requirements.data ?? null;
  const briefIs404 =
    requirements.error instanceof ApiError && requirements.error.status === 404;

  return (
    <div className="space-y-14 py-14">
      <Header tender={tender.data} />
      <Description description={tender.data.description} />
      <Sidebar tender={tender.data} />
      <BriefBlock
        tenderId={id}
        brief={brief}
        is404={briefIs404}
        otherError={!brief && !briefIs404 && requirements.error ? requirements.error : null}
        onReload={() => requirements.mutate()}
      />
      <DocumentsSection
        files={documents.data ?? null}
        loading={!documents.data && !documents.error}
        error={documents.error}
        onRetry={() => documents.mutate()}
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
// Brief block (requirements)
// ---------------------------------------------------------------------------

function BriefBlock({
  tenderId,
  brief,
  is404,
  otherError,
  onReload,
}: {
  tenderId: number;
  brief: TenderRequirements | null;
  is404: boolean;
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
    return <GenerateBriefCta tenderId={tenderId} onSuccess={onReload} />;
  }

  return (
    <section className="space-y-10">
      <div className="section-eyebrow">Brief</div>

      {brief.recommendation && <RecommendationBanner brief={brief} />}
      {brief.risk_flags && brief.risk_flags.length > 0 && <RiskFlags flags={brief.risk_flags} />}

      {brief.summary && (
        <div
          className="max-w-3xl text-text"
          style={{ fontSize: "16px", lineHeight: 1.7 }}
        >
          {brief.summary}
        </div>
      )}

      <EvaluationCriteriaCards items={brief.evaluation_criteria} />

      <RequirementsList
        title="Mandatory requirements"
        items={brief.mandatory_requirements}
        prominence="primary"
      />
      <RequirementsList
        title="Desired requirements"
        items={brief.desired_requirements}
        prominence="secondary"
      />

      <DocumentsRequiredList items={brief.documents_required} />
      <QuestionsList items={brief.questions_to_answer} />

      {(brief.estimated_effort_days || brief.model) && (
        <footer
          className="flex flex-wrap gap-6 border-t border-border pt-6 text-text-dim"
          style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
        >
          {brief.estimated_effort_days != null && (
            <span>Estimated effort: {brief.estimated_effort_days} days</span>
          )}
          {brief.model && <span>Model: {brief.model}</span>}
          {brief.extracted_at && (
            <span>Extracted: {formatDate(brief.extracted_at)}</span>
          )}
        </footer>
      )}
    </section>
  );
}

function GenerateBriefCta({
  tenderId,
  onSuccess,
}: {
  tenderId: number;
  onSuccess: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trigger = async () => {
    setBusy(true);
    setError(null);
    try {
      const apiBase =
        process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "/__api";
      const res = await fetch(`${apiBase}/admin/extract-requirements/${tenderId}`, {
        method: "POST",
      });
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new Error(body || `HTTP ${res.status}`);
      }
      onSuccess();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to generate brief");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section
      className="featured-glow-line relative overflow-hidden rounded-2xl border border-border-strong bg-bg-elevated p-10 backdrop-blur-md"
      style={{ borderRadius: "24px" }}
    >
      <PillKicker withDot={false}>Brief draft</PillKicker>
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
        Run the requirements extractor over the attached documents to produce
        a structured brief: mandatory and desired requirements, evaluation
        criteria, risk flags, and a recommendation.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={trigger}
          disabled={busy}
          className="btn-primary"
        >
          {busy ? "Generating…" : "Generate brief"}
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

function RecommendationBanner({ brief }: { brief: TenderRequirements }) {
  if (!brief.recommendation) return null;
  const r = brief.recommendation.toLowerCase();
  const palette =
    r === "pursue"
      ? { border: "border-mint/40", bg: "bg-mint/10", text: "text-mint" }
      : r === "decline"
        ? { border: "border-danger/40", bg: "bg-danger/10", text: "text-danger" }
        : { border: "border-amber/40", bg: "bg-amber/10", text: "text-amber" };
  return (
    <div
      role="status"
      className={`rounded-2xl border ${palette.border} ${palette.bg} p-6 backdrop-blur-md`}
      style={{ borderRadius: "18px" }}
    >
      <div
        className={`${palette.text}`}
        style={{
          fontSize: "11px",
          fontWeight: 500,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
          marginBottom: "10px",
        }}
      >
        Recommendation · {brief.recommendation}
      </div>
      {brief.recommendation_reason && (
        <p className="text-text" style={{ fontSize: "15px", lineHeight: 1.6 }}>
          {brief.recommendation_reason}
        </p>
      )}
    </div>
  );
}

function RiskFlags({ flags }: { flags: string[] }) {
  return (
    <div
      role="note"
      aria-label="Risk flags"
      className="rounded-xl border border-amber/30 bg-amber/5 p-5"
      style={{ borderRadius: "18px" }}
    >
      <div
        className="text-amber mb-3"
        style={{
          fontSize: "11px",
          fontWeight: 500,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
        }}
      >
        ⚠ Risk flags
      </div>
      <ul className="space-y-2">
        {flags.map((f, i) => (
          <li key={`${i}-${f}`} className="text-text" style={{ fontSize: "14px", lineHeight: 1.55 }}>
            · {f}
          </li>
        ))}
      </ul>
    </div>
  );
}

function EvaluationCriteriaCards({
  items,
}: {
  items: EvaluationCriterion[] | null;
}) {
  if (!items?.length) return null;
  return (
    <section className="space-y-4">
      <h3 className="font-display text-text" style={{ fontSize: "22px", letterSpacing: "-0.02em" }}>
        Evaluation criteria
      </h3>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {items.map((c, i) => (
          <div
            key={`${i}-${c.name}`}
            className="rounded-xl border border-border bg-bg-elevated p-5 backdrop-blur-sm"
            style={{ borderRadius: "18px" }}
          >
            <div className="flex items-baseline justify-between gap-3 mb-2">
              <span className="text-text" style={{ fontWeight: 500 }}>
                {c.name}
              </span>
              {c.weight != null && (
                <span className="display-num text-mint-pale" style={{ fontSize: "20px" }}>
                  {c.weight}%
                </span>
              )}
            </div>
            {c.description && (
              <p className="text-text-muted" style={{ fontSize: "13px", lineHeight: 1.55 }}>
                {c.description}
              </p>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function RequirementsList({
  title,
  items,
  prominence,
}: {
  title: string;
  items: RequirementItem[] | null;
  prominence: "primary" | "secondary";
}) {
  if (!items?.length) return null;
  return (
    <section className="space-y-4">
      <h3
        className={`font-display ${prominence === "primary" ? "text-text" : "text-text-muted"}`}
        style={{
          fontSize: prominence === "primary" ? "24px" : "20px",
          letterSpacing: "-0.02em",
        }}
      >
        {title}
      </h3>
      <ol className="space-y-3">
        {items.map((req, i) => (
          <li
            key={i}
            className={`flex gap-5 ${
              prominence === "primary"
                ? "rounded-xl border border-border bg-bg-elevated p-5 backdrop-blur-sm"
                : "border-l border-border pl-5 py-2"
            }`}
            style={prominence === "primary" ? { borderRadius: "18px" } : undefined}
          >
            <span
              className="display-num text-mint-pale shrink-0"
              style={{ fontSize: "20px", lineHeight: 1.4, width: "32px" }}
              aria-hidden="true"
            >
              {String(i + 1).padStart(2, "0")}
            </span>
            <div className="flex-1 min-w-0">
              <div className="flex items-start justify-between gap-3">
                <p
                  className={prominence === "primary" ? "text-text" : "text-text-muted"}
                  style={{ fontSize: "15px", lineHeight: 1.55 }}
                >
                  {req.text}
                </p>
                {req.confidence && <ConfidenceChip confidence={req.confidence} />}
              </div>
              {(req.category || req.evidence_required) && (
                <div
                  className="mt-2 flex flex-wrap gap-3 text-text-dim font-mono"
                  style={{ fontSize: "10px", letterSpacing: "0.1em", textTransform: "uppercase" }}
                >
                  {req.category && <span>cat: {req.category}</span>}
                  {req.evidence_required && (
                    <span>evidence: {req.evidence_required}</span>
                  )}
                </div>
              )}
              {req.source_excerpt && (
                <blockquote
                  className="mt-3 border-l-2 border-border-strong pl-3 italic text-text-dim"
                  style={{ fontSize: "13px", lineHeight: 1.5 }}
                >
                  &ldquo;{req.source_excerpt}&rdquo;
                </blockquote>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
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
      className={`shrink-0 whitespace-nowrap rounded-pill border px-2 py-0.5 font-mono ${tone}`}
      style={{ fontSize: "10px", letterSpacing: "0.1em", textTransform: "uppercase" }}
      aria-label={`Confidence: ${confidence}`}
    >
      {confidence}
    </span>
  );
}

function DocumentsRequiredList({
  items,
}: {
  items: DocumentRequired[] | null;
}) {
  if (!items?.length) return null;
  return (
    <section className="space-y-4">
      <h3 className="font-display text-text" style={{ fontSize: "22px", letterSpacing: "-0.02em" }}>
        Documents required
      </h3>
      <ul className="space-y-3">
        {items.map((doc, i) => (
          <li
            key={`${i}-${doc.name}`}
            className="flex items-start gap-4 rounded-xl border border-border bg-bg-elevated p-5"
            style={{ borderRadius: "18px" }}
          >
            <span
              className="font-mono text-text-muted shrink-0"
              style={{
                fontSize: "10px",
                padding: "4px 8px",
                background: "rgba(255, 255, 255, 0.04)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: "4px",
                letterSpacing: "0.08em",
                textTransform: "uppercase",
              }}
            >
              {classifyDocumentKind(doc.name)}
            </span>
            <div className="flex-1 min-w-0">
              <div className="flex flex-wrap items-baseline gap-3">
                <p className="text-text" style={{ fontSize: "15px" }}>
                  {doc.name}
                </p>
                {doc.mandatory && (
                  <span
                    className="font-mono text-amber"
                    style={{
                      fontSize: "10px",
                      padding: "2px 7px",
                      border: "1px solid rgba(245, 166, 35, 0.4)",
                      borderRadius: "4px",
                      letterSpacing: "0.1em",
                      textTransform: "uppercase",
                    }}
                  >
                    Mandatory
                  </span>
                )}
              </div>
              {doc.description && (
                <p className="text-text-muted mt-2" style={{ fontSize: "13px", lineHeight: 1.55 }}>
                  {doc.description}
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function classifyDocumentKind(name: string): string {
  const n = name.toLowerCase();
  if (/insurance|liability|indemnity/.test(n)) return "insurance";
  if (/\biso\b|cert(if(ication)?)?/.test(n)) return "iso/cert";
  if (/policy|policies/.test(n)) return "policy";
  if (/case\s*stud(y|ies)|reference/.test(n)) return "case study";
  if (/financ(es|ial)|account|turnover/.test(n)) return "financial";
  if (/cv|bio|profile/.test(n)) return "cv";
  if (/method|approach|statement/.test(n)) return "method";
  return "other";
}

function QuestionsList({ items }: { items: QuestionToAnswer[] | null }) {
  if (!items?.length) return null;
  return (
    <section className="space-y-4">
      <h3 className="font-display text-text" style={{ fontSize: "22px", letterSpacing: "-0.02em" }}>
        Questions to answer
      </h3>
      <ol className="space-y-4">
        {items.map((q, i) => (
          <li
            key={i}
            className="border-l-2 border-mint/40 pl-5 space-y-2"
          >
            <p className="text-text" style={{ fontSize: "15px", lineHeight: 1.55 }}>
              {q.question}
            </p>
            <div
              className="flex flex-wrap gap-3 text-text-dim font-mono"
              style={{ fontSize: "10px", letterSpacing: "0.1em", textTransform: "uppercase" }}
            >
              {q.word_limit != null && <span>{q.word_limit} words max</span>}
              {q.weight != null && <span>weight {q.weight}%</span>}
              {q.section && <span>section: {q.section}</span>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Documents (downloaded files)
// ---------------------------------------------------------------------------

function DocumentsSection({
  files,
  loading,
  error,
  onRetry,
}: {
  files: TenderDocumentFile[] | null;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <section className="space-y-4">
      <div className="section-eyebrow">Attached documents</div>
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
          No documents downloaded yet.
        </p>
      ) : (
        <ul className="space-y-2.5">
          {files.map((f) => (
            <li key={f.id}>
              <DocumentRow file={f} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function DocumentRow({ file }: { file: TenderDocumentFile }) {
  return (
    <div
      className="flex items-center gap-4 rounded-xl border border-border bg-bg-elevated p-5 backdrop-blur-sm"
      style={{ borderRadius: "18px" }}
    >
      <DownloadStatusBadge status={file.download_status} />
      <div className="flex-1 min-w-0">
        <a
          href={file.url}
          target="_blank"
          rel="noopener noreferrer"
          className="block truncate text-text hover:text-mint-pale focus:outline-none"
          style={{ fontSize: "14px" }}
          aria-label={`Open ${file.title ?? "document"} (opens in new tab)`}
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
