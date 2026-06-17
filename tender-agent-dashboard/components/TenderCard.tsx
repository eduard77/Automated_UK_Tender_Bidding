import Link from "next/link";

import { type Tender } from "@/lib/api";
import {
  daysUntil,
  formatCurrency,
  formatCurrencyCompact,
  relativeDeadline,
  shortDeadline,
  timeOnly,
} from "@/lib/format";

import SectorBadge from "./SectorBadge";

type Variant = "default" | "priority" | "featured";

export default function TenderCard({
  tender,
  variant = "default",
}: {
  tender: Tender;
  variant?: Variant;
}) {
  const days = daysUntil(tender.deadline_at);
  // "priority" is auto-determined; caller can also force it.
  const isPriority =
    variant === "priority" || (variant === "default" && days !== null && days >= 0 && days <= 14);
  const deadline = relativeDeadline(tender.deadline_at);

  if (variant === "featured") {
    // Featured card variant is rendered by app/page.tsx directly because it
    // takes too many extra slots (excerpt, value-label, two buttons) to be
    // worth shoving through this prop API. Keep this branch for type safety.
    return null;
  }

  return (
    <Link
      href={`/tenders/${tender.id}`}
      className={[
        "group flex flex-col rounded-xl border p-7 backdrop-blur-md transition-all duration-200 outline-none",
        "focus-visible:ring-2 focus-visible:ring-mint focus-visible:ring-offset-2 focus-visible:ring-offset-bg",
        isPriority
          ? "border-amber/25 bg-bg-elevated-priority hover:border-amber/50"
          : "border-border bg-bg-elevated hover:border-mint-pale/30 hover:-translate-y-px",
      ].join(" ")}
      style={{ borderRadius: "18px" }}
    >
      {/* Meta row: source · notice type · optional urgent badge */}
      <div className="flex flex-wrap items-center gap-2.5 mb-[18px]">
        <span
          className="font-mono text-text-muted"
          style={{
            fontSize: "11px",
            fontWeight: 500,
            letterSpacing: "0.08em",
            padding: "3px 8px",
            background: "rgba(255, 255, 255, 0.04)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "4px",
          }}
        >
          {tender.source_code}
        </span>
        {tender.notice_type && (
          <span
            className="text-text-dim"
            style={{
              fontSize: "11px",
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              fontWeight: 500,
            }}
          >
            {tender.notice_type}
          </span>
        )}
        {tender.primary_sector && <SectorBadge sector={tender.primary_sector} />}
        {isPriority && days !== null && days >= 0 && (
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
            {days === 0 ? "Closes today" : `Closes in ${days} day${days === 1 ? "" : "s"}`}
          </span>
        )}
      </div>

      <h3
        className="font-display text-text mb-3"
        style={{
          fontSize: "22px",
          fontWeight: 400,
          letterSpacing: "-0.018em",
          lineHeight: 1.18,
          fontVariationSettings: '"opsz" 60',
        }}
      >
        {tender.title}
      </h3>

      {tender.buyer_name && (
        <p className="text-[14px] leading-[1.4] text-text-muted mb-[22px]">
          <span className="text-text" style={{ fontWeight: 450 }}>
            {tender.buyer_name}
          </span>
        </p>
      )}

      {tender.cpv_codes && tender.cpv_codes.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-4">
          {tender.cpv_codes.slice(0, 3).map((code) => (
            <span
              key={code}
              className="font-mono text-text-muted"
              style={{
                fontSize: "10px",
                padding: "3px 7px",
                background: "rgba(255, 255, 255, 0.04)",
                borderRadius: "3px",
                letterSpacing: "0.02em",
              }}
            >
              {code}
            </span>
          ))}
          {tender.cpv_codes.length > 3 && (
            <span
              className="font-mono text-text-dim"
              style={{
                fontSize: "10px",
                padding: "3px 7px",
                background: "rgba(255, 255, 255, 0.04)",
                borderRadius: "3px",
                letterSpacing: "0.02em",
              }}
            >
              +{tender.cpv_codes.length - 3}
            </span>
          )}
        </div>
      )}

      <div className="mt-auto flex flex-wrap items-center justify-between gap-4 border-t border-border pt-[18px]">
        <div className="flex flex-col gap-0.5">
          <CardValue
            amount={tender.value_amount}
            currency={tender.value_currency}
          />
        </div>
        <div className="flex flex-col items-end gap-0.5">
          <span
            className={`display-num ${deadline.urgent ? "text-amber" : "text-text"}`}
            style={{
              fontSize: "22px",
              letterSpacing: "-0.01em",
              lineHeight: 1,
            }}
          >
            {tender.deadline_at ? shortDeadline(tender.deadline_at) : "TBC"}
          </span>
          <span
            className="text-text-dim"
            style={{
              fontSize: "10px",
              letterSpacing: "0.12em",
              textTransform: "uppercase",
              marginTop: "6px",
            }}
          >
            {tender.deadline_at ? timeOnly(tender.deadline_at) : "Provisional"}
          </span>
        </div>
      </div>
    </Link>
  );
}

function CardValue({
  amount,
  currency,
}: {
  amount: string | number | null;
  currency: string | null;
}) {
  const full = formatCurrency(amount, currency);
  if (full === null) {
    return (
      <>
        <span
          className="display-num italic text-text-dim"
          style={{ fontSize: "15px", lineHeight: 1 }}
        >
          Value not stated
        </span>
        <span
          className="text-text-dim"
          style={{
            fontSize: "10px",
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            marginTop: "6px",
          }}
        >
          via portal
        </span>
      </>
    );
  }
  const compact = formatCurrencyCompact(amount, currency);
  const useCompact = (typeof amount === "string" ? Number(amount) : amount ?? 0) >= 100_000_000;
  return (
    <>
      <span
        className="display-num text-mint-pale"
        style={{
          fontSize: "22px",
          letterSpacing: "-0.015em",
          lineHeight: 1,
        }}
        title={full}
      >
        {useCompact ? compact : full}
      </span>
      <span
        className="text-text-dim"
        style={{
          fontSize: "10px",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          marginTop: "6px",
        }}
      >
        Estimated value
      </span>
    </>
  );
}
