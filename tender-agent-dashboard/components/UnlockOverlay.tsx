"use client";

import { useEffect, useState } from "react";
import {
  fetchBillingStatus,
  fetchSubmissionFee,
  startCheckout,
  type BillingStatus,
  type SubmissionFeeQuote,
} from "@/lib/api";

interface Props {
  tenderId: number;
  /** What's locked here — drives the headline copy. */
  surface: "brief" | "document";
  /** Should the package CTA appear? Only on the tender detail. */
  showPackageCta?: boolean;
}

/**
 * Chunk 6 — the "Unlock full brief & documents" CTA that floats over the
 * 50%-preview content. Backend is the source of truth: this component never
 * tries to render full content; it only offers the unlock paths.
 *
 * If payments aren't configured (no Stripe keys in .env), the pay buttons
 * gracefully say "Coming soon" — the app stays usable for browsing.
 */
export function UnlockOverlay({ tenderId, surface, showPackageCta }: Props) {
  const [status, setStatus] = useState<BillingStatus | null>(null);
  const [fee, setFee] = useState<SubmissionFeeQuote | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    fetchBillingStatus().then(setStatus).catch(() => setStatus(null));
    if (showPackageCta) {
      fetchSubmissionFee(tenderId).then(setFee).catch(() => setFee(null));
    }
  }, [tenderId, showPackageCta]);

  const configured = status?.payments_configured ?? false;

  async function checkout(
    kind: "payg_brief" | "plan_100" | "plan_unlimited" | "submission_package",
  ) {
    setBusy(kind);
    try {
      const res = await startCheckout(kind, tenderId);
      if (res.status === "ok" && res.url) {
        window.location.assign(res.url);
        return;
      }
      // payments_not_configured — surface the message.
      alert(res.message ?? "Payments aren't configured yet. Try again later.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="rounded-xl border border-slate-300/50 bg-slate-900/60 backdrop-blur-sm p-6 text-slate-100 shadow-lg">
      <h3 className="text-lg font-semibold mb-1">
        {surface === "brief"
          ? "Unlock full brief & documents"
          : "Unlock full document"}
      </h3>
      <p className="text-sm text-slate-300 mb-4">
        You're seeing the first half. Unlock the full brief, full documents,
        and downloads for this tender.
      </p>
      <div className="grid gap-2 sm:grid-cols-3">
        <CtaButton
          label={configured ? "Single brief — £10" : "Single brief (coming soon)"}
          disabled={!configured || busy !== null}
          loading={busy === "payg_brief"}
          onClick={() => checkout("payg_brief")}
        />
        <CtaButton
          label={configured ? "Plan 100 — £100/mo" : "Plan 100 (coming soon)"}
          disabled={!configured || busy !== null}
          loading={busy === "plan_100"}
          onClick={() => checkout("plan_100")}
        />
        <CtaButton
          label={
            configured ? "Unlimited — £250/mo" : "Unlimited (coming soon)"
          }
          disabled={!configured || busy !== null}
          loading={busy === "plan_unlimited"}
          onClick={() => checkout("plan_unlimited")}
        />
      </div>
      {showPackageCta && fee && (
        <div className="mt-4 border-t border-slate-700 pt-3 text-sm">
          <p className="mb-2 text-slate-300">
            Generating a submission package costs an upfront 0.5% fee
            (clamped £100–£300) for THIS tender.
          </p>
          <CtaButton
            label={
              !configured
                ? "Generate package (coming soon)"
                : fee.already_paid
                  ? "Package paid — generate"
                  : `Generate package — £${fee.amount_gbp.toFixed(0)}`
            }
            disabled={!configured || busy !== null}
            loading={busy === "submission_package"}
            onClick={() => checkout("submission_package")}
          />
        </div>
      )}
    </div>
  );
}

function CtaButton({
  label,
  disabled,
  loading,
  onClick,
}: {
  label: string;
  disabled?: boolean;
  loading?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="rounded-md bg-amber-400 hover:bg-amber-300 disabled:bg-slate-700 disabled:text-slate-400 px-3 py-2 text-sm font-medium text-slate-900 transition"
    >
      {loading ? "…" : label}
    </button>
  );
}
