"use client";

import type { PortalDetail } from "@/lib/api";

function asString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}
function asBool(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}
function asNum(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export default function ClassificationDisplay({ portal }: { portal: PortalDetail }) {
  const data = portal.classification_data ?? null;
  const status = data ? asString(data._status) : null;

  if (!data || status === "fetch_failed") {
    return (
      <div
        className="rounded-2xl border p-7 backdrop-blur-md"
        style={{
          borderRadius: "18px",
          background: "rgba(245, 166, 35, 0.05)",
          borderColor: "rgba(245, 166, 35, 0.22)",
        }}
      >
        <h3
          className="font-display text-text mb-2"
          style={{ fontSize: "20px", letterSpacing: "-0.015em" }}
        >
          {status === "fetch_failed"
            ? "Homepage fetch failed."
            : "Not yet classified."}
        </h3>
        <p className="text-text-muted" style={{ fontSize: "14px", lineHeight: 1.55 }}>
          {status === "fetch_failed"
            ? "We couldn't reach this domain at the time of classification. Re-run when the site is back."
            : "Classification will run automatically the next time this portal is seen in a tender, or trigger it manually below."}
        </p>
      </div>
    );
  }

  if (status === "claude_unavailable") {
    return (
      <div
        className="rounded-2xl border p-7 backdrop-blur-md"
        style={{
          borderRadius: "18px",
          background: "rgba(248, 113, 113, 0.05)",
          borderColor: "rgba(248, 113, 113, 0.22)",
        }}
      >
        <h3
          className="font-display text-text mb-2"
          style={{ fontSize: "20px", letterSpacing: "-0.015em" }}
        >
          Claude was unavailable.
        </h3>
        <p className="text-text-muted" style={{ fontSize: "14px", lineHeight: 1.55 }}>
          The homepage fetched OK but the classifier didn't return a parseable
          response. Re-run when the API is back.
        </p>
      </div>
    );
  }

  const isPortal = asBool(data.is_procurement_portal);
  const confidence = asNum(data.confidence);
  const reasoning = asString(data.reasoning);
  const notes = asString(data.notes);
  const suggestedName = asString(data.suggested_display_name);
  const suggestedLogin = asString(data.login_type);
  const suggestedPriority = asString(data.suggested_priority);

  return (
    <div
      className="rounded-2xl border border-border bg-bg-elevated p-7 backdrop-blur-md space-y-6"
      style={{ borderRadius: "18px" }}
    >
      <div className="flex items-baseline gap-4 flex-wrap">
        <h3
          className="font-display text-text"
          style={{ fontSize: "22px", letterSpacing: "-0.02em" }}
        >
          Claude classification
        </h3>
        <span
          className="text-text-dim"
          style={{ fontSize: "11px", letterSpacing: "0.12em", textTransform: "uppercase" }}
        >
          {isPortal === null
            ? "No verdict"
            : isPortal
              ? "Procurement portal"
              : "Not a portal"}
        </span>
      </div>

      {confidence !== null && (
        <div>
          <div
            className="flex justify-between text-text-dim mb-2"
            style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
          >
            <span>Confidence</span>
            <span>{Math.round(confidence * 100)}%</span>
          </div>
          <div
            className="overflow-hidden"
            style={{
              height: "6px",
              background: "rgba(255,255,255,0.05)",
              borderRadius: "999px",
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${Math.round(Math.max(0, Math.min(1, confidence)) * 100)}%`,
                background:
                  confidence >= 0.7
                    ? "rgba(151, 213, 188, 0.85)"
                    : confidence >= 0.4
                      ? "rgba(245, 166, 35, 0.7)"
                      : "rgba(248, 113, 113, 0.7)",
              }}
            />
          </div>
        </div>
      )}

      {reasoning && (
        <div>
          <div
            className="text-text-dim mb-2"
            style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
          >
            Reasoning
          </div>
          <p
            className="text-text"
            style={{ fontSize: "14px", lineHeight: 1.55 }}
          >
            {reasoning}
          </p>
        </div>
      )}

      {notes && (
        <div>
          <div
            className="text-text-dim mb-2"
            style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase" }}
          >
            Notes
          </div>
          <p
            className="text-text-muted"
            style={{ fontSize: "13px", lineHeight: 1.55 }}
          >
            {notes}
          </p>
        </div>
      )}

      {(suggestedName || suggestedLogin || suggestedPriority) && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 pt-4 border-t border-border/60">
          {suggestedName && (
            <Field label="Suggested name" value={suggestedName} />
          )}
          {suggestedLogin && (
            <Field label="Login type" value={suggestedLogin} />
          )}
          {suggestedPriority && (
            <Field label="Priority" value={suggestedPriority} />
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div
        className="text-text-dim mb-1"
        style={{ fontSize: "10px", letterSpacing: "0.12em", textTransform: "uppercase" }}
      >
        {label}
      </div>
      <div className="text-text" style={{ fontSize: "14px" }}>
        {value}
      </div>
    </div>
  );
}
