"use client";

import Link from "next/link";
import useSWR from "swr";

import PillKicker from "./PillKicker";
import PortalStatusBadge from "./PortalStatusBadge";
import StatBlock from "./StatBlock";
import { type PortalPlatform, fetcher } from "@/lib/api";

export default function PlatformsList() {
  const { data, error, isLoading } = useSWR<PortalPlatform[]>(
    "/platforms",
    fetcher,
    { revalidateOnFocus: true },
  );

  const totalPortals = (data ?? []).reduce(
    (acc, p) => acc + p.buyer_instance_count,
    0,
  );
  const transactional = (data ?? []).filter((p) => p.kind === "transactional").length;
  const special = (data ?? []).filter((p) => p.kind === "special").length;

  return (
    <div className="space-y-12 py-14">
      <header className="space-y-6">
        <PillKicker>Platform registry · adapter targets</PillKicker>
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
          One adapter, many buyers.
        </h1>
        <p
          className="text-text-muted"
          style={{ fontSize: "17px", lineHeight: 1.6, maxWidth: "720px" }}
        >
          UK tenders cluster onto a handful of e-tendering products. Build one
          adapter per platform and it serves every buyer hosted on it — that is
          the leverage behind Phase 4.
        </p>
      </header>

      {data && data.length > 0 && (
        <div className="flex flex-wrap gap-x-14 gap-y-6 border-y border-border py-8">
          <StatBlock label="Platforms" value={data.length.toString()} unit="tracked" />
          <StatBlock
            label="Transactional"
            value={transactional.toString()}
            unit="vendors"
            tone="mint"
          />
          <StatBlock label="Special" value={special.toString()} unit="vendors" />
          <StatBlock
            label="Buyer instances"
            value={totalPortals.toString()}
            unit="portals"
          />
        </div>
      )}

      {error ? (
        <div
          role="alert"
          className="rounded-xl border border-danger/40 bg-danger/5 p-5 text-text"
          style={{ borderRadius: "18px", fontSize: "14px" }}
        >
          Failed to load platforms.
        </div>
      ) : isLoading && !data ? (
        <div className="text-text-dim" style={{ fontSize: "14px" }}>
          Loading…
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          {data?.map((p) => <PlatformCard key={p.id} platform={p} />)}
        </div>
      )}
    </div>
  );
}

function PlatformCard({ platform }: { platform: PortalPlatform }) {
  return (
    <Link
      href={`/platforms/${platform.slug}`}
      className={[
        "group flex flex-col rounded-xl border p-7 backdrop-blur-md transition-all duration-200 outline-none",
        "focus-visible:ring-2 focus-visible:ring-mint focus-visible:ring-offset-2 focus-visible:ring-offset-bg",
        "border-border bg-bg-elevated hover:border-mint-pale/30 hover:-translate-y-px",
      ].join(" ")}
      style={{ borderRadius: "18px" }}
    >
      <div className="flex flex-wrap items-center gap-2.5 mb-[18px]">
        <span
          className="text-text-dim"
          style={{
            fontSize: "11px",
            letterSpacing: "0.1em",
            textTransform: "uppercase",
            fontWeight: 500,
          }}
        >
          {platform.vendor}
        </span>
        <span
          className="ml-auto text-text-dim"
          style={{
            fontSize: "10px",
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            fontWeight: 500,
            padding: "2px 8px",
            borderRadius: "999px",
            border: "1px solid rgba(255,255,255,0.12)",
          }}
        >
          {platform.kind}
        </span>
      </div>

      <h3
        className="font-display text-text mb-4"
        style={{
          fontSize: "24px",
          letterSpacing: "-0.02em",
          fontWeight: 400,
          fontVariationSettings: '"opsz" 72',
        }}
      >
        {platform.display_name}
      </h3>

      <div className="flex flex-wrap items-center gap-2 mb-5">
        <span
          className="inline-flex items-center gap-1.5 text-mint-pale"
          style={{
            fontSize: "12px",
            fontWeight: 500,
            padding: "3px 9px",
            borderRadius: "999px",
            background: "rgba(151, 213, 188, 0.08)",
            border: "1px solid rgba(151, 213, 188, 0.22)",
          }}
        >
          {platform.total_tender_count} tenders
        </span>
        <span className="text-text-dim" style={{ fontSize: "12px" }}>
          {platform.buyer_instance_count} buyer
          {platform.buyer_instance_count === 1 ? "" : "s"}
        </span>
      </div>

      <div className="mt-auto">
        <PortalStatusBadge status={platform.adapter_status} />
      </div>
    </Link>
  );
}
