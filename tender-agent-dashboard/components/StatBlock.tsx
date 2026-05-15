// Label/value pair from the .stats-row pattern. Value is Fraunces, label is
// tiny uppercase muted. Unit is a small Inter inline suffix.

export default function StatBlock({
  label,
  value,
  unit,
  tone = "default",
}: {
  label: string;
  value: React.ReactNode;
  unit?: string;
  tone?: "default" | "mint";
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span
        className="text-text-dim"
        style={{
          fontSize: "11px",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          fontWeight: 500,
        }}
      >
        {label}
      </span>
      <span
        className={`display-num ${tone === "mint" ? "text-mint-pale" : "text-text"}`}
        style={{
          fontSize: "36px",
          letterSpacing: "-0.02em",
          lineHeight: 1,
        }}
      >
        {value}
        {unit && (
          <span
            className="text-text-muted font-body"
            style={{
              fontSize: "14px",
              fontWeight: 450,
              marginLeft: "6px",
              letterSpacing: 0,
            }}
          >
            {unit}
          </span>
        )}
      </span>
    </div>
  );
}
