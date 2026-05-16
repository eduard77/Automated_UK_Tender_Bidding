"use client";

import { useEffect } from "react";

import PillKicker from "@/components/PillKicker";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // eslint-disable-next-line no-console
    console.error("Dashboard error boundary fired:", error);
  }, [error]);

  return (
    <div className="py-24">
      <div
        role="alert"
        className="mx-auto max-w-xl rounded-2xl border border-danger/40 bg-bg-elevated p-14 backdrop-blur-md"
        style={{ borderRadius: "24px" }}
      >
        <PillKicker withDot={false}>Unexpected error</PillKicker>
        <h1
          className="font-display text-text mt-6"
          style={{
            fontSize: "clamp(36px, 5vw, 48px)",
            letterSpacing: "-0.03em",
            lineHeight: 1.04,
          }}
        >
          Something broke on this page.
        </h1>
        <p
          className="text-text-muted mt-5"
          style={{ fontSize: "15px", lineHeight: 1.55 }}
        >
          The error has been logged to your browser console. You can try
          reloading; if it keeps happening, the backend or a recent dashboard
          change is at fault.
        </p>
        {error.message && (
          <p
            className="mt-4 rounded-md border border-border-strong bg-bg p-3 font-mono text-text-muted"
            style={{ fontSize: "12px" }}
          >
            {error.message}
            {error.digest && (
              <>
                <br />
                <span className="text-text-dim">digest: {error.digest}</span>
              </>
            )}
          </p>
        )}
        <div className="mt-7 flex gap-3 flex-wrap">
          <button type="button" onClick={reset} className="btn-primary">
            ↻ Try again
          </button>
          <a href="/" className="btn-secondary">
            Today desk
          </a>
        </div>
      </div>
    </div>
  );
}
