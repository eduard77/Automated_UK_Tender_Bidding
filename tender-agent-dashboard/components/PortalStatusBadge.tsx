import type { AdapterStatus } from "@/lib/api";
import { adapterStatusLabel } from "@/lib/api";

const STYLES: Record<AdapterStatus, { fg: string; bg: string; border: string }> = {
  not_started: {
    fg: "rgba(255, 255, 255, 0.55)",
    bg: "rgba(255, 255, 255, 0.04)",
    border: "rgba(255, 255, 255, 0.12)",
  },
  stub: {
    fg: "#f5a623",
    bg: "rgba(245, 166, 35, 0.08)",
    border: "rgba(245, 166, 35, 0.32)",
  },
  read_only: {
    fg: "rgba(151, 213, 188, 0.95)",
    bg: "rgba(151, 213, 188, 0.08)",
    border: "rgba(151, 213, 188, 0.32)",
  },
  full: {
    fg: "#021410",
    bg: "rgba(151, 213, 188, 0.92)",
    border: "rgba(151, 213, 188, 0.92)",
  },
  deprecated: {
    fg: "#f87171",
    bg: "rgba(248, 113, 113, 0.08)",
    border: "rgba(248, 113, 113, 0.28)",
  },
};

export default function PortalStatusBadge({
  status,
}: {
  status: AdapterStatus;
}) {
  const s = STYLES[status];
  return (
    <span
      className="inline-flex items-center gap-1.5"
      style={{
        fontSize: "11px",
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        fontWeight: 500,
        padding: "4px 10px",
        borderRadius: "999px",
        color: s.fg,
        background: s.bg,
        border: `1px solid ${s.border}`,
      }}
    >
      {adapterStatusLabel(status)}
    </span>
  );
}
