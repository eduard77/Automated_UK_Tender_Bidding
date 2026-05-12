"use client";

import { useEffect, useState } from "react";
import { isSubscribed, subscribePush } from "@/lib/push";

export default function PushBell({ vapidPublicKey }: { vapidPublicKey: string }) {
  const [subbed, setSubbed] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { isSubscribed().then(setSubbed); }, []);

  const onClick = async () => {
    setBusy(true); setErr(null);
    try {
      await subscribePush(vapidPublicKey);
      setSubbed(true);
    } catch (e: any) {
      setErr(e?.message || "Failed");
    } finally {
      setBusy(false);
    }
  };

  if (subbed === null) return null;

  return (
    <div className="flex items-center gap-3">
      {subbed ? (
        <span className="text-xs font-mono uppercase tracking-wider text-sage">
          ◉ alerts on
        </span>
      ) : (
        <button
          onClick={onClick}
          disabled={busy || !vapidPublicKey}
          className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 border border-bone/30 hover:border-rust hover:text-rust transition disabled:opacity-50"
        >
          {busy ? "…" : "→ enable alerts"}
        </button>
      )}
      {err && <span className="text-xs text-oxblood">{err}</span>}
    </div>
  );
}
