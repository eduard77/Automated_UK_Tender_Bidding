import type { Priority } from "@/lib/api";
import { priorityLabel } from "@/lib/api";

const STYLES: Record<Priority, { fg: string; bg: string; border: string }> = {
  critical: {
    fg: "#f87171",
    bg: "rgba(248, 113, 113, 0.10)",
    border: "rgba(248, 113, 113, 0.35)",
  },
  high: {
    fg: "#f5a623",
    bg: "rgba(245, 166, 35, 0.10)",
    border: "rgba(245, 166, 35, 0.35)",
  },
  medium: {
    fg: "rgba(151, 213, 188, 0.92)",
    bg: "rgba(151, 213, 188, 0.08)",
    border: "rgba(151, 213, 188, 0.30)",
  },
  low: {
    fg: "rgba(255, 255, 255, 0.55)",
    bg: "rgba(255, 255, 255, 0.04)",
    border: "rgba(255, 255, 255, 0.12)",
  },
};

export default function PortalPriorityBadge({ priority }: { priority: Priority }) {
  const s = STYLES[priority];
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
      {priorityLabel(priority)}
    </span>
  );
}
