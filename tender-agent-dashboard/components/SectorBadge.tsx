// Phase 3b: the primary-sector label shown on a tender card / result row, so
// the user can see why a tender matched the sector filter. Mint-tinted to
// distinguish it from the neutral source/CPV chips.

export default function SectorBadge({ sector }: { sector: string }) {
  return (
    <span
      className="inline-flex items-center text-mint-pale"
      style={{
        fontSize: "11px",
        fontWeight: 500,
        letterSpacing: "0.04em",
        padding: "2px 8px",
        background: "rgba(122, 230, 191, 0.1)",
        border: "1px solid rgba(122, 230, 191, 0.28)",
        borderRadius: "100px",
      }}
      title={`Primary sector: ${sector}`}
    >
      {sector}
    </span>
  );
}
