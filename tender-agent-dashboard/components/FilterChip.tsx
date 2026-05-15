// Filter chip — pill-shaped tag with optional muted label prefix and 'x'
// remove control. Three visual states: default / active / add (dashed).
//
// Mirrors the .filter-chip pattern from the v2 mockup.

type ChipVariant = "default" | "active" | "add";

export default function FilterChip({
  label,
  value,
  onRemove,
  onClick,
  variant = "default",
  type = "button",
  className = "",
}: {
  label?: string;
  value?: React.ReactNode;
  onRemove?: () => void;
  onClick?: () => void;
  variant?: ChipVariant;
  type?: "button" | "submit";
  className?: string;
}) {
  const base =
    "inline-flex items-center gap-2 rounded-pill border px-3.5 py-2 text-[13px] font-normal transition-colors backdrop-blur-sm focus-visible:outline-mint";
  const variantClass =
    variant === "active"
      ? "border-mint/40 bg-mint/10 text-mint-pale"
      : variant === "add"
        ? "border-dashed border-border-strong bg-transparent text-text-muted hover:text-text hover:border-mint"
        : "border-border-strong bg-bg-elevated text-text hover:border-mint-pale/40";

  if (variant === "add") {
    return (
      <button
        type={type}
        onClick={onClick}
        className={`${base} ${variantClass} ${className}`.trim()}
      >
        {value ?? "+ Add filter"}
      </button>
    );
  }

  return (
    <span className={`${base} ${variantClass} ${className}`.trim()}>
      {label && <span className="text-text-muted">{label}:</span>}
      {value}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          className="ml-0.5 border-l border-border-strong pl-2 text-text-dim hover:text-text"
          aria-label={`Remove filter${label ? ` ${label}` : ""}`}
        >
          ×
        </button>
      )}
    </span>
  );
}
