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

  // On mount: check browser support, fetch the VAPID key from the API (single
  // source of truth — no env var on the client), then check existing subscription.
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

  const onSubscribe = async () => {
    if (state.kind !== "off") return;
    setBusy(true);
    setErr(null);
    try {
      await subscribePush(state.vapidKey);
      setState({ kind: "on", vapidKey: state.vapidKey });
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  const onUnsubscribe = async () => {
    if (state.kind !== "on") return;
    setBusy(true);
    setErr(null);
    try {
      await unsubscribePush();
      setState({ kind: "off", vapidKey: state.vapidKey });
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  // Render nothing while loading, while push isn't supported, and when the
  // backend reports push isn't configured. The bell is purely additive UI.
  if (state.kind === "loading" || state.kind === "unsupported" || state.kind === "unconfigured") {
    return null;
  }

  return (
    <div className="flex items-center gap-3">
      {state.kind === "on" ? (
        <button
          type="button"
          onClick={onUnsubscribe}
          disabled={busy}
          aria-label="Disable push alerts"
          className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 border border-sage text-sage hover:bg-sage/10 transition disabled:opacity-50 focus:outline-none focus:ring-1 focus:ring-sage"
        >
          {busy ? "…" : "◉ alerts on"}
        </button>
      ) : (
        <button
          type="button"
          onClick={onSubscribe}
          disabled={busy}
          aria-label="Enable push alerts"
          className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 border border-bone/30 hover:border-rust hover:text-rust transition disabled:opacity-50 focus:outline-none focus:ring-1 focus:ring-rust"
        >
          {busy ? "…" : "→ enable alerts"}
        </button>
      )}
      {err && (
        <span role="alert" className="text-xs text-oxblood">
          {err}
        </span>
      )}
    </div>
  );
}
