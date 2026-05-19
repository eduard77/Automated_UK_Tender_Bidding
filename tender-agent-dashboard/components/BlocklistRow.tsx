import type { BlocklistEntry } from "@/lib/api";
import { relativeAge } from "@/lib/format";

export default function BlocklistRow({
  entry,
  onRemove,
  busy,
}: {
  entry: BlocklistEntry;
  onRemove: () => void;
  busy: boolean;
}) {
  return (
    <tr className="border-t border-border/40">
      <td className="py-3 pr-4">
        <span
          className="font-mono text-text"
          style={{ fontSize: "13px" }}
        >
          {entry.domain}
        </span>
      </td>
      <td className="py-3 pr-4 text-text-muted" style={{ fontSize: "13px" }}>
        {entry.reason ?? "—"}
      </td>
      <td className="py-3 pr-4 text-text-dim" style={{ fontSize: "12px" }}>
        {entry.added_by}
      </td>
      <td className="py-3 pr-4 text-text-dim" style={{ fontSize: "12px" }}>
        {relativeAge(entry.added_at) ?? "—"}
      </td>
      <td className="py-3 text-right">
        <button
          type="button"
          onClick={onRemove}
          disabled={busy}
          className="text-danger hover:text-text disabled:opacity-50"
          style={{ fontSize: "13px" }}
        >
          Remove
        </button>
      </td>
    </tr>
  );
}
