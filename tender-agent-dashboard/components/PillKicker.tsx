// Small uppercase tracked-letter label, mint pale, optionally with a pulsing
// dot to signal "live". Matches the .kicker pattern from the v2 mockup.

export default function PillKicker({
  children,
  withDot = true,
  className = "",
}: {
  children: React.ReactNode;
  withDot?: boolean;
  className?: string;
}) {
  return (
    <span className={`pill-kicker ${className}`.trim()}>
      {withDot && (
        <span
          aria-hidden="true"
          className="inline-block h-1.5 w-1.5 rounded-full bg-mint"
          style={{ animation: "pulse 2.4s ease-in-out infinite" }}
        />
      )}
      {children}
    </span>
  );
}
