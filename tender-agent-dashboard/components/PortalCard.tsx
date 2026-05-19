import Link from "next/link";

import { type Portal, loginTypeLabel } from "@/lib/api";
import { relativeAge } from "@/lib/format";

import PortalPriorityBadge from "./PortalPriorityBadge";
import PortalStatusBadge from "./PortalStatusBadge";

export default function PortalCard({ portal }: { portal: Portal }) {
  return (
    <Link
      href={`/portals/${portal.id}`}
      className={[
        "group flex flex-col rounded-xl border p-7 backdrop-blur-md transition-all duration-200 outline-none",
        "focus-visible:ring-2 focus-visible:ring-mint focus-visible:ring-offset-2 focus-visible:ring-offset-bg",
        "border-border bg-bg-elevated hover:border-mint-pale/30 hover:-translate-y-px",
      ].join(" ")}
      style={{ borderRadius: "18px" }}
    >
      {/* Domain pill */}
      <div className="flex flex-wrap items-center gap-2.5 mb-[18px]">
        <span
          className="font-mono text-text-muted truncate"
          style={{
            fontSize: "11px",
            fontWeight: 500,
            letterSpacing: "0.04em",
            padding: "3px 8px",
            background: "rgba(255, 255, 255, 0.04)",
            border: "1px solid rgba(255, 255, 255, 0.08)",
            borderRadius: "4px",
            maxWidth: "100%",
          }}
          title={portal.domain}
        >
          {portal.domain}
        </span>
        <span className="ml-auto">
          <PortalPriorityBadge priority={portal.priority} />
        </span>
      </div>

      {/* Display name */}
      <h3
        className="font-display text-text mb-3 truncate"
        style={{
          fontSize: "22px",
          letterSpacing: "-0.02em",
          fontWeight: 400,
          fontVariationSettings: '"opsz" 72',
        }}
        title={portal.display_name}
      >
        {portal.display_name}
      </h3>

      {/* Counts + login type */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
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
          {portal.tender_count} tender{portal.tender_count === 1 ? "" : "s"}
        </span>
        <span
          className="text-text-dim"
          style={{ fontSize: "12px" }}
        >
          {loginTypeLabel(portal.login_type)}
        </span>
      </div>

      {/* Status */}
      <div className="mb-5">
        <PortalStatusBadge status={portal.adapter_status} />
      </div>

      {/* Footer strip */}
      <div
        className="flex items-center justify-between pt-4 border-t border-border/60 mt-auto"
        style={{ fontSize: "11px", color: "rgba(255,255,255,0.45)" }}
      >
        <span>First seen {relativeAge(portal.first_seen_at) ?? "—"}</span>
        <span>Last seen {relativeAge(portal.last_seen_at) ?? "—"}</span>
      </div>
    </Link>
  );
}
