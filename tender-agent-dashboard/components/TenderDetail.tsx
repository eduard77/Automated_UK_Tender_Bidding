"use client";

import Link from "next/link";
import useSWR from "swr";

import {
  ApiError,
  daysUntil,
  fetcher,
  formatValue,
  type DocumentRequired,
  type EvaluationCriterion,
  type QuestionToAnswer,
  type RequirementItem,
  type Tender,
  type TenderDocumentFile,
  type TenderRequirements,
} from "@/lib/api";

const SOURCE_LABEL: Record<string, string> = {
  FTS: "Find a Tender",
  CF: "Contracts Finder",
  PCS: "Public Contracts Scotland",
  S2W: "Sell2Wales",
  NI: "eTendersNI",
};

export default function TenderDetail({ id }: { id: number }) {
  const tender = useSWR<Tender>(`/tenders/${id}`, fetcher, {
    revalidateOnFocus: false,
  });
  const requirements = useSWR<TenderRequirements>(
    `/tenders/${id}/requirements`,
    fetcher,
    {
      revalidateOnFocus: false,
      // Don't retry 404 — that's a known "not yet extracted" state.
      shouldRetryOnError: (err) => !(err instanceof ApiError && err.status === 404),
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
    return <SkeletonDetail />;
  }

  const brief = requirements.data ?? null;
  const briefIs404 =
    requirements.error instanceof ApiError && requirements.error.status === 404;

  return (
    <article className="space-y-8">
      <Header tender={tender.data} />
      <Brief
        brief={brief}
        is404={briefIs404}
        otherError={!brief && !briefIs404 && requirements.error ? requirements.error : null}
        onRetry={() => requirements.mutate()}
      />
      <AttachedDocuments
        files={documents.data ?? null}
        loading={!documents.data && !documents.error}
        error={documents.error}
        onRetry={() => documents.mutate()}
      />
    </article>
  );
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function Header({ tender }: { tender: Tender }) {
  const days = daysUntil(tender.deadline_at);
  const urgent = days !== null && days >= 0 && days <= 7;
  return (
    <header className="space-y-3 border-b border-bone/10 pb-6">
      <div className="flex flex-wrap items-center gap-2 text-[10px] font-mono uppercase tracking-widest">
        <SourceBadge tender={tender} />
        {tender.notice_type && (
          <>
            <span className="text-bone/30">·</span>
            <span className="text-bone/50">{tender.notice_type}</span>
          </>
        )}
        {tender.status && tender.status !== "active" && (
          <>
            <span className="text-bone/30">·</span>
            <span className="text-sage/80">{tender.status}</span>
          </>
        )}
      </div>

      <h1 className="font-display text-3xl md:text-4xl text-bone leading-tight">
        {tender.title}
      </h1>

      {tender.buyer_name && (
        <p className="italic text-bone/70">{tender.buyer_name}</p>
      )}

      <div className="flex flex-wrap items-center gap-4 pt-2">
        <div className="font-mono text-lg text-bone">
          {formatValue(tender.value_amount, tender.value_currency)}
        </div>
        {days !== null && (
          <span
            className={`text-xs font-mono uppercase tracking-wider px-2 py-1 border ${
              urgent
                ? "border-rust text-rust bg-rust/10"
                : days < 0
                  ? "border-bone/20 text-bone/40"
                  : "border-bone/30 text-bone/70"
            }`}
          >
            {days < 0 ? "closed" : days === 0 ? "today" : `${days}d left`}
          </span>
        )}
        {tender.source_url && (
          <a
            href={tender.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs font-mono uppercase tracking-wider px-4 py-2 border border-bone/30 text-bone hover:border-rust hover:text-rust transition focus:outline-none focus:ring-1 focus:ring-rust"
          >
            ↗ open in source portal
          </a>
        )}
      </div>

      {tender.description && (
        <p className="text-sm text-bone/70 max-w-3xl pt-4 leading-relaxed">
          {tender.description}
        </p>
      )}
    </header>
  );
}

function SourceBadge({ tender }: { tender: Tender }) {
  const label = SOURCE_LABEL[tender.source_code] || tender.source_code;
  const content = <span className="text-rust">{label}</span>;
  if (!tender.source_url) return content;
  return (
    <a
      href={tender.source_url}
      target="_blank"
      rel="noopener noreferrer"
      className="text-rust hover:underline"
      aria-label={`${label} (opens in new tab)`}
    >
      {label} ↗
    </a>
  );
}

// ---------------------------------------------------------------------------
// Brief (requirements)
// ---------------------------------------------------------------------------

function Brief({
  brief,
  is404,
  otherError,
  onRetry,
}: {
  brief: TenderRequirements | null;
  is404: boolean;
  otherError: unknown;
  onRetry: () => void;
}) {
  if (otherError) {
    return (
      <section className="border border-oxblood/60 bg-oxblood/10 p-5 space-y-2">
        <h2 className="font-display text-lg text-bone">Couldn't load brief</h2>
        <button
          type="button"
          onClick={onRetry}
          className="text-xs font-mono uppercase tracking-wider px-4 py-2 border border-bone/30 text-bone hover:border-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
        >
          ↻ retry
        </button>
      </section>
    );
  }

  if (is404 || !brief) {
    return <NotYetExtractedCallout />;
  }

  return (
    <section className="space-y-6" aria-label="Tender brief">
      <RiskFlags flags={brief.risk_flags} />
      <RecommendationBanner brief={brief} />
      {brief.summary && (
        <div className="text-sm text-bone/85 leading-relaxed max-w-3xl">
          {brief.summary}
        </div>
      )}
      <EvaluationCriteriaList items={brief.evaluation_criteria} />
      <RequirementsList
        title="Mandatory requirements"
        items={brief.mandatory_requirements}
        prominence="primary"
        emptyText="No mandatory requirements extracted."
      />
      <RequirementsList
        title="Desired requirements"
        items={brief.desired_requirements}
        prominence="secondary"
        emptyText="No desired requirements extracted."
      />
      <DocumentsRequiredList items={brief.documents_required} />
      <QuestionsList items={brief.questions_to_answer} />
      {(brief.estimated_effort_days || brief.model) && (
        <footer className="text-[10px] font-mono uppercase tracking-wider text-bone/40 pt-2 border-t border-bone/10 flex flex-wrap gap-4">
          {brief.estimated_effort_days != null && (
            <span>estimated effort: {brief.estimated_effort_days} days</span>
          )}
          {brief.model && <span>model: {brief.model}</span>}
          {brief.extracted_at && (
            <span>extracted: {formatDate(brief.extracted_at)}</span>
          )}
        </footer>
      )}
    </section>
  );
}

function NotYetExtractedCallout() {
  return (
    <section className="border border-rust/40 bg-rust/5 p-5 space-y-3">
      <h2 className="font-display text-lg text-bone">Brief not yet generated</h2>
      <p className="text-sm text-bone/70 max-w-2xl">
        Documents haven't been parsed yet for this tender. Generating a brief runs
        the requirements extractor over the attached documents and produces a
        structured summary — mandatory and desired requirements, risk flags, and
        a recommendation.
      </p>
      <button
        type="button"
        onClick={() => {
          // Wired in T5 (extractor validation + admin trigger).
          // eslint-disable-next-line no-console
          console.log("Generate brief: not yet wired");
        }}
        className="text-xs font-mono uppercase tracking-wider px-4 py-2 border border-rust text-rust hover:bg-rust hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
      >
        → generate brief
      </button>
    </section>
  );
}

function RiskFlags({ flags }: { flags: string[] | null }) {
  if (!flags?.length) return null;
  return (
    <div
      role="note"
      aria-label="Risk flags"
      className="border border-rust/60 bg-rust/10 p-4 space-y-2"
    >
      <div className="text-[10px] font-mono uppercase tracking-widest text-rust">
        ⚠ risk flags
      </div>
      <ul className="space-y-1">
        {flags.map((flag, i) => (
          <li key={`${i}-${flag}`} className="text-sm text-bone/90">
            · {flag}
          </li>
        ))}
      </ul>
    </div>
  );
}

function RecommendationBanner({ brief }: { brief: TenderRequirements }) {
  if (!brief.recommendation) return null;
  const r = brief.recommendation.toLowerCase();
  const palette =
    r === "pursue"
      ? { border: "border-sage/60", bg: "bg-sage/10", text: "text-sage" }
      : r === "decline"
        ? { border: "border-oxblood/60", bg: "bg-oxblood/15", text: "text-oxblood" }
        : { border: "border-rust/50", bg: "bg-rust/10", text: "text-rust" };
  return (
    <div
      role="status"
      className={`border ${palette.border} ${palette.bg} p-4 space-y-2`}
    >
      <div className={`text-[10px] font-mono uppercase tracking-widest ${palette.text}`}>
        recommendation · {brief.recommendation}
      </div>
      {brief.recommendation_reason && (
        <p className="text-sm text-bone/85 leading-relaxed">
          {brief.recommendation_reason}
        </p>
      )}
    </div>
  );
}

function EvaluationCriteriaList({ items }: { items: EvaluationCriterion[] | null }) {
  if (!items?.length) return null;
  return (
    <section className="space-y-2">
      <h3 className="font-display text-base text-bone">Evaluation criteria</h3>
      <ul className="space-y-2">
        {items.map((c, i) => (
          <li
            key={`${i}-${c.name}`}
            className="flex items-baseline justify-between gap-4 border-b border-bone/5 pb-2"
          >
            <div>
              <div className="text-sm text-bone">{c.name}</div>
              {c.description && (
                <div className="text-xs text-bone/55 mt-0.5">{c.description}</div>
              )}
            </div>
            {c.weight != null && (
              <span className="text-xs font-mono text-bone/70 whitespace-nowrap">
                {c.weight}%
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function RequirementsList({
  title,
  items,
  prominence,
  emptyText,
}: {
  title: string;
  items: RequirementItem[] | null;
  prominence: "primary" | "secondary";
  emptyText: string;
}) {
  if (!items?.length) {
    return (
      <section className="space-y-2">
        <h3
          className={`font-display ${
            prominence === "primary" ? "text-lg text-bone" : "text-base text-bone/70"
          }`}
        >
          {title}
        </h3>
        <p className="text-xs text-bone/40 italic">{emptyText}</p>
      </section>
    );
  }
  return (
    <section className="space-y-2">
      <h3
        className={`font-display ${
          prominence === "primary" ? "text-lg text-bone" : "text-base text-bone/70"
        }`}
      >
        {title}
      </h3>
      <ul className="space-y-3">
        {items.map((req, i) => (
          <li
            key={i}
            className={`${
              prominence === "primary"
                ? "border border-bone/10 bg-ink-soft/30 p-3"
                : "border-l border-bone/10 pl-3"
            }`}
          >
            <div className="flex items-start justify-between gap-3">
              <p
                className={`text-sm leading-snug ${
                  prominence === "primary" ? "text-bone/90" : "text-bone/70"
                }`}
              >
                {req.text}
              </p>
              {req.confidence && <ConfidenceChip confidence={req.confidence} />}
            </div>
            {(req.category || req.evidence_required) && (
              <div className="mt-1.5 text-[10px] font-mono uppercase tracking-wider text-bone/40 flex flex-wrap gap-3">
                {req.category && <span>cat: {req.category}</span>}
                {req.evidence_required && <span>evidence: {req.evidence_required}</span>}
              </div>
            )}
            {req.source_excerpt && (
              <blockquote className="mt-2 text-xs text-bone/50 italic border-l-2 border-bone/10 pl-2">
                "{req.source_excerpt}"
              </blockquote>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function ConfidenceChip({ confidence }: { confidence: "low" | "medium" | "high" }) {
  const palette =
    confidence === "high"
      ? "border-sage/60 text-sage"
      : confidence === "medium"
        ? "border-rust/60 text-rust"
        : "border-oxblood/60 text-oxblood";
  return (
    <span
      className={`text-[10px] font-mono uppercase tracking-wider px-1.5 py-0.5 border whitespace-nowrap ${palette}`}
      aria-label={`Confidence: ${confidence}`}
    >
      {confidence}
    </span>
  );
}

function DocumentsRequiredList({ items }: { items: DocumentRequired[] | null }) {
  if (!items?.length) return null;
  return (
    <section className="space-y-2">
      <h3 className="font-display text-lg text-bone">Documents required</h3>
      <ul className="space-y-2">
        {items.map((doc, i) => (
          <li
            key={`${i}-${doc.name}`}
            className="flex items-start gap-3 border border-bone/10 p-3 bg-ink-soft/30"
          >
            <KindBadge name={doc.name} />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <p className="text-sm text-bone">{doc.name}</p>
                {doc.mandatory && (
                  <span className="text-[10px] font-mono uppercase tracking-wider text-rust border border-rust/50 px-1.5 py-0.5">
                    mandatory
                  </span>
                )}
              </div>
              {doc.description && (
                <p className="text-xs text-bone/55 mt-1">{doc.description}</p>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function KindBadge({ name }: { name: string }) {
  const kind = classifyDocumentKind(name);
  return (
    <span
      className="text-[10px] font-mono uppercase tracking-wider text-bone/60 border border-bone/20 px-2 py-1 whitespace-nowrap self-start"
      aria-label={`Document kind: ${kind}`}
    >
      {kind}
    </span>
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
  if (/method|approach|statement/.test(n)) return "method statement";
  return "other";
}

function QuestionsList({ items }: { items: QuestionToAnswer[] | null }) {
  if (!items?.length) return null;
  return (
    <section className="space-y-2">
      <h3 className="font-display text-lg text-bone">Questions to answer</h3>
      <ol className="space-y-3">
        {items.map((q, i) => (
          <li key={i} className="border-l-2 border-rust/30 pl-4 space-y-1">
            <p className="text-sm text-bone/90 leading-snug">{q.question}</p>
            <div className="flex flex-wrap gap-3 text-[10px] font-mono uppercase tracking-wider text-bone/40">
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
// Attached documents (downloaded files)
// ---------------------------------------------------------------------------

function AttachedDocuments({
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
    <section className="space-y-3">
      <h3 className="font-display text-lg text-bone">Attached documents</h3>
      {error ? (
        <div role="alert" className="border border-oxblood/60 bg-oxblood/10 p-3 text-sm text-bone flex items-center justify-between">
          <span>Couldn't load documents.</span>
          <button
            type="button"
            onClick={onRetry}
            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 border border-bone/30 text-bone hover:border-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
          >
            ↻ retry
          </button>
        </div>
      ) : loading ? (
        <ul className="space-y-2" aria-busy="true">
          {Array.from({ length: 3 }).map((_, i) => (
            <li key={i} className="h-14 border border-bone/10 bg-ink-soft/30 animate-pulse" />
          ))}
        </ul>
      ) : !files?.length ? (
        <p className="text-sm text-bone/40 italic">No documents downloaded yet.</p>
      ) : (
        <ul className="space-y-2">
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
    <div className="border border-bone/10 bg-ink-soft/30 p-3 flex items-center gap-3">
      <DownloadStatusBadge status={file.download_status} />
      <div className="flex-1 min-w-0">
        <a
          href={file.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm text-bone hover:text-rust transition truncate block focus:outline-none focus:ring-1 focus:ring-rust"
          aria-label={`Open ${file.title ?? "document"} (opens in new tab)`}
        >
          {file.title || file.url}
        </a>
        {file.error && (
          <p className="text-xs text-oxblood mt-1 truncate">{file.error}</p>
        )}
      </div>
      <div className="flex items-center gap-2 whitespace-nowrap">
        {file.format && (
          <span className="text-[10px] font-mono uppercase tracking-wider text-bone/60 border border-bone/20 px-1.5 py-0.5">
            {file.format}
          </span>
        )}
        {file.bytes != null && (
          <span className="text-[10px] font-mono text-bone/50">
            {formatBytes(file.bytes)}
          </span>
        )}
        {file.sha256 && (
          <span
            className="text-[10px] font-mono text-bone/30"
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
  const palette =
    s === "ok" || s === "success"
      ? "border-sage/60 text-sage"
      : s === "error" || s === "failed"
        ? "border-oxblood/60 text-oxblood"
        : "border-bone/20 text-bone/50";
  return (
    <span
      className={`text-[10px] font-mono uppercase tracking-wider px-1.5 py-0.5 border whitespace-nowrap ${palette}`}
      aria-label={`Download status: ${status}`}
    >
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------
// States + helpers
// ---------------------------------------------------------------------------

function SkeletonDetail() {
  return (
    <div className="space-y-6 animate-pulse" aria-busy="true" aria-label="Loading tender">
      <div className="space-y-3 border-b border-bone/10 pb-6">
        <div className="h-3 w-40 bg-bone/10" />
        <div className="h-9 w-3/4 bg-bone/15" />
        <div className="h-4 w-1/3 bg-bone/10" />
        <div className="flex gap-4">
          <div className="h-6 w-24 bg-bone/10" />
          <div className="h-6 w-20 bg-bone/10" />
        </div>
      </div>
      <div className="space-y-2">
        <div className="h-4 w-1/4 bg-bone/10" />
        <div className="h-3 w-full bg-bone/5" />
        <div className="h-3 w-5/6 bg-bone/5" />
      </div>
    </div>
  );
}

function NotFound() {
  return (
    <div className="border border-bone/10 bg-ink-soft/30 p-8 text-center space-y-3">
      <h1 className="font-display text-2xl text-bone">Tender not found</h1>
      <p className="text-sm text-bone/60 max-w-md mx-auto">
        Nothing exists at this ID. It may have been deduplicated into another
        record, or it was never imported.
      </p>
      <Link
        href="/"
        className="inline-block text-xs font-mono uppercase tracking-wider px-4 py-2 border border-rust text-rust hover:bg-rust hover:text-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
      >
        ← back to tenders
      </Link>
    </div>
  );
}

function TopLevelError({ onRetry }: { onRetry: () => void }) {
  return (
    <div role="alert" className="border border-oxblood/60 bg-oxblood/10 p-6 space-y-3">
      <h1 className="font-display text-lg text-bone">Couldn't load this tender</h1>
      <p className="text-sm text-bone/70">The backend didn't respond. Retry?</p>
      <button
        type="button"
        onClick={onRetry}
        className="text-xs font-mono uppercase tracking-wider px-4 py-2 border border-bone/30 text-bone hover:border-bone transition focus:outline-none focus:ring-1 focus:ring-rust"
      >
        ↻ retry
      </button>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}
