"use client";

import { useEffect, useState } from "react";

import {
  getVapidPublicKey,
  isSubscribed,
  subscribePush,
  unsubscribePush,
} from "@/lib/push";

type State =
  | { kind: "loading" }
  | { kind: "unsupported" }
  | { kind: "off"; vapidKey: string }
  | { kind: "on"; vapidKey: string }
  | { kind: "unconfigured" };

export default function PushBell() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (
        typeof window === "undefined" ||
        !("serviceWorker" in navigator) ||
        !("PushManager" in window) ||
        !("Notification" in window)
      ) {
        if (!cancelled) setState({ kind: "unsupported" });
        return;
      }
      const key = await getVapidPublicKey();
      if (cancelled) return;
      if (!key) {
        setState({ kind: "unconfigured" });
        return;
      }
      const subbed = await isSubscribed();
      if (cancelled) return;
      setState(subbed ? { kind: "on", vapidKey: key } : { kind: "off", vapidKey: key });
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Hide the bell while loading or when push isn't available — it's additive UI.
  if (state.kind === "loading" || state.kind === "unsupported" || state.kind === "unconfigured") {
    return null;
  }

  const subscribed = state.kind === "on";

  const toggle = async () => {
    setBusy(true);
    setErr(null);
    try {
      if (state.kind === "on") {
        await unsubscribePush();
        setState({ kind: "off", vapidKey: state.vapidKey });
      } else {
        await subscribePush(state.vapidKey);
        setState({ kind: "on", vapidKey: state.vapidKey });
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={toggle}
        disabled={busy}
        aria-label={subscribed ? "Disable push alerts" : "Enable push alerts"}
        aria-pressed={subscribed}
        title={subscribed ? "Push alerts on — click to disable" : "Enable push alerts"}
        className="relative flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-bg-elevated text-text-muted transition-colors hover:border-border-strong hover:text-text disabled:opacity-50"
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
          <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
        </svg>
        {subscribed && (
          <span
            aria-hidden="true"
            className="absolute right-[10px] top-[9px] h-2 w-2 rounded-full bg-mint"
            style={{
              boxShadow: "0 0 0 2px #07111a",
              animation: "pulse 2.4s ease-in-out infinite",
            }}
          />
        )}
      </button>
      {err && (
        <span
          role="alert"
          className="absolute right-0 top-12 whitespace-nowrap rounded-md border border-danger/40 bg-bg-elevated-strong px-2 py-1 text-xs text-danger"
        >
          {err}
        </span>
      )}
    </div>
  );
}
