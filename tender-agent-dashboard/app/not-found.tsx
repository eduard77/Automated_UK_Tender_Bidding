import Link from "next/link";

import PillKicker from "@/components/PillKicker";

export default function NotFound() {
  return (
    <div className="py-24">
      <div
        className="mx-auto max-w-xl rounded-2xl border border-border bg-bg-elevated p-14 text-center backdrop-blur-md"
        style={{ borderRadius: "24px" }}
      >
        <PillKicker withDot={false}>404 · Not found</PillKicker>
        <h1
          className="font-display text-text mt-6"
          style={{
            fontSize: "clamp(40px, 5.5vw, 56px)",
            letterSpacing: "-0.03em",
            lineHeight: 1.04,
          }}
        >
          Off the map.
        </h1>
        <p
          className="text-text-muted mt-5 mx-auto max-w-md"
          style={{ fontSize: "15px", lineHeight: 1.55 }}
        >
          Nothing exists at this URL. Try the today desk, or jump to filters
          to set up a fresh bid profile.
        </p>
        <div className="mt-8 flex justify-center gap-3 flex-wrap">
          <Link href="/" className="btn-primary">
            Today desk <span className="arrow">→</span>
          </Link>
          <Link href="/filters" className="btn-secondary">
            Filters
          </Link>
        </div>
      </div>
    </div>
  );
}
