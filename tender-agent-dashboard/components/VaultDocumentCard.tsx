"use client";

import Link from "next/link";

import {
  isPlaceholderTitle,
  vaultCategoryLabel,
  type VaultDocument,
} from "@/lib/api";
import { daysUntil, formatCurrency, formatDate } from "@/lib/format";

export default function VaultDocumentCard({
  doc,
  onArchive,
}: {
  doc: VaultDocument;
  onArchive?: () => void;
}) {
  const version = doc.current_version;
  const summary = version ? oneLineSummary(version.claims) : "No version uploaded yet";
  const isPlaceholder = isPlaceholderTitle(doc.title);
  const expiry = version?.expiry_date ?? null;
  const days = expiry ? daysUntil(expiry) : null;
  const expiringSoon = days !== null && days >= 0 && days <= 90;
  const expired = days !== null && days < 0;

  return (
    <article
      className={[
        "group flex flex-col rounded-xl border p-7 backdrop-blur-md transition-all duration-200",
        isPlaceholder
          ? "border-amber/25 bg-bg-elevated-priority hover:border-amber/45"
          : expired
            ? "border-danger/30 bg-bg-elevated hover:border-danger/60"
            : "border-border bg-bg-elevated hover:border-mint-pale/30 hover:-translate-y-px",
      ].join(" ")}
      style={{ borderRadius: "18px" }}
    >
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <span
          className="text-text"
          style={{
            fontSize: "11px",
            fontWeight: 500,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            padding: "3px 9px",
            background: "rgba(255, 255, 255, 0.04)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "5px",
          }}
        >
          {vaultCategoryLabel(doc.category)}
        </span>
        {doc.subcategory && (
          <span
            className="text-text-dim"
            style={{
              fontSize: "11px",
              letterSpacing: "0.08em",
              textTransform: "uppercase",
            }}
          >
            {doc.subcategory.replaceAll("_", " ")}
          </span>
        )}
        {isPlaceholder && (
          <span
            className="ml-auto text-amber"
            style={{
              fontSize: "10px",
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              fontWeight: 500,
            }}
          >
            Placeholder
          </span>
        )}
        {!isPlaceholder && expiringSoon && days !== null && (
          <span
            className="ml-auto inline-flex items-center gap-1.5 text-amber"
            style={{
              fontSize: "11px",
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              fontWeight: 500,
            }}
          >
            <span className="inline-block h-[5px] w-[5px] rounded-full bg-amber" />
            Expires in {days} day{days === 1 ? "" : "s"}
          </span>
        )}
        {!isPlaceholder && expired && (
          <span
            className="ml-auto text-danger"
            style={{
              fontSize: "11px",
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              fontWeight: 500,
            }}
          >
            Expired
          </span>
        )}
      </div>

      <Link
        href={`/vault/${doc.id}`}
        className="font-display text-text outline-none transition-colors hover:text-mint-pale focus-visible:text-mint-pale"
        style={{
          fontSize: "22px",
          fontWeight: 400,
          letterSpacing: "-0.018em",
          lineHeight: 1.18,
          fontVariationSettings: '"opsz" 60',
        }}
      >
        {strippedTitle(doc.title)}
      </Link>

      <p
        className="text-text-muted mt-3"
        style={{ fontSize: "14px", lineHeight: 1.5 }}
      >
        {summary}
      </p>

      <div className="mt-auto flex flex-wrap items-center justify-between gap-4 border-t border-border pt-5">
        <div className="flex flex-col gap-0.5">
          <span
            className="text-text"
            style={{ fontSize: "14px" }}
          >
            v{version?.version ?? 1}
            {version?.uploaded_at && (
              <span className="ml-2 text-text-dim">
                · {formatDate(version.uploaded_at)}
              </span>
            )}
          </span>
        </div>
        <div className="flex gap-2">
          <Link href={`/vault/${doc.id}`} className="btn-ghost">
            View
          </Link>
          {onArchive && (
            <button type="button" onClick={onArchive} className="btn-danger">
              Archive
            </button>
          )}
        </div>
      </div>
    </article>
  );
}

function strippedTitle(title: string): string {
  if (title.startsWith("PLACEHOLDER -- ")) return title.slice("PLACEHOLDER -- ".length);
  if (title.startsWith("PLACEHOLDER — ")) return title.slice("PLACEHOLDER — ".length);
  return title;
}

function oneLineSummary(claims: unknown): string {
  if (!claims || typeof claims !== "object") return "No claims yet";
  const c = claims as Record<string, unknown>;
  const dt = c.doc_type;
  const fmtDate = (v: unknown) => (typeof v === "string" ? formatDate(v) : null);
  if (dt === "insurance_certificate") {
    const bits: string[] = [];
    if (c.insurance_type) bits.push(String(c.insurance_type).replaceAll("_", " "));
    const amt = formatCurrency(c.cover_amount as string | number | null, (c.currency as string) ?? "GBP");
    if (amt) bits.push(amt);
    const exp = fmtDate(c.valid_until);
    if (exp) bits.push(`expires ${exp}`);
    return bits.length > 0 ? bits.join(" · ") : "Insurance certificate";
  }
  if (dt === "iso_certificate") {
    const bits: string[] = [];
    if (c.standard) bits.push(String(c.standard));
    if (c.scope) bits.push(String(c.scope));
    const exp = fmtDate(c.valid_until);
    if (exp) bits.push(`valid to ${exp}`);
    return bits.length > 0 ? bits.join(" · ") : "ISO certificate";
  }
  if (dt === "policy") {
    const bits: string[] = [];
    if (c.policy_kind) bits.push(String(c.policy_kind).replaceAll("_", " "));
    if (c.signed_by_director) bits.push("signed by director");
    const due = fmtDate(c.review_due);
    if (due) bits.push(`review due ${due}`);
    return bits.length > 0 ? bits.join(" · ") : "Policy";
  }
  if (dt === "case_study") {
    const bits: string[] = [];
    if (c.client) bits.push(String(c.client));
    if (c.client_sector) bits.push(String(c.client_sector).replaceAll("_", " "));
    const val = formatCurrency(c.value as string | number | null, (c.currency as string) ?? "GBP");
    if (val) bits.push(val);
    return bits.length > 0 ? bits.join(" · ") : "Case study";
  }
  if (dt === "accounts") {
    const bits: string[] = [];
    const fy = fmtDate(c.fiscal_year_end);
    if (fy) bits.push(`Y/E ${fy}`);
    const t = formatCurrency(c.turnover as string | number | null, (c.currency as string) ?? "GBP");
    if (t) bits.push(`turnover ${t}`);
    if (c.audited === true) bits.push("audited");
    return bits.length > 0 ? bits.join(" · ") : "Annual accounts";
  }
  return "Unclassified document";
}
