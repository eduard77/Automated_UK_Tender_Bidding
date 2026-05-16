// Display helpers for the dashboard. Centralised so every monetary value and
// every deadline renders identically across components.

const GBP_FORMATTER = new Intl.NumberFormat("en-GB", {
  style: "currency",
  currency: "GBP",
  maximumFractionDigits: 0,
});

const CURRENCY_FORMATTERS = new Map<string, Intl.NumberFormat>();

function getCurrencyFormatter(currency: string): Intl.NumberFormat {
  let f = CURRENCY_FORMATTERS.get(currency);
  if (!f) {
    f = new Intl.NumberFormat("en-GB", {
      style: "currency",
      currency,
      maximumFractionDigits: 0,
    });
    CURRENCY_FORMATTERS.set(currency, f);
  }
  return f;
}

/** Full currency representation: £4,000,000,000. */
export function formatCurrency(
  amount: string | number | null | undefined,
  currency: string | null | undefined,
): string | null {
  if (amount === null || amount === undefined || amount === "") return null;
  const n = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(n)) return null;
  const formatter =
    !currency || currency === "GBP" ? GBP_FORMATTER : getCurrencyFormatter(currency);
  return formatter.format(n);
}

/** Short representation: £4bn / £16.5M / £500k. */
export function formatCurrencyCompact(
  amount: string | number | null | undefined,
  currency: string | null | undefined,
): string | null {
  if (amount === null || amount === undefined || amount === "") return null;
  const n = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(n)) return null;
  const symbol = currency === "GBP" || !currency ? "£" : `${currency} `;
  const abs = Math.abs(n);
  if (abs >= 1_000_000_000) return `${symbol}${(n / 1_000_000_000).toFixed(1)}bn`;
  if (abs >= 1_000_000) return `${symbol}${(n / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `${symbol}${Math.round(n / 1_000)}k`;
  return `${symbol}${n.toLocaleString("en-GB")}`;
}

/** Days between now and an ISO timestamp. Null when input is empty/invalid. */
export function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.ceil((t - Date.now()) / 86_400_000);
}

/** "21 days" / "Closes in 5 days" / "Closed". */
export function relativeDeadline(iso: string | null | undefined): {
  text: string;
  urgent: boolean;
  closed: boolean;
} {
  const d = daysUntil(iso);
  if (d === null) return { text: "TBC", urgent: false, closed: false };
  if (d < 0) return { text: "Closed", urgent: false, closed: true };
  if (d === 0) return { text: "Closes today", urgent: true, closed: false };
  if (d <= 14) return { text: `Closes in ${d} days`, urgent: true, closed: false };
  return { text: `${d} days`, urgent: false, closed: false };
}

/** "17 Jun · 12:00" — day, short month, 24h time. */
export function absoluteDeadline(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const datePart = d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  const timePart = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${datePart} · ${timePart}`;
}

/** "17 Jun 2026" — for non-deadline dates (published_at, extracted_at). */
export function formatDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** Short date for tender cards — "25 May" or "TBC". */
export function shortDeadline(iso: string | null | undefined): string {
  if (!iso) return "TBC";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "TBC";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** "Closes 12:00" — extracted from the same ISO. */
export function timeOnly(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `Closes ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false })}`;
}

/** "minutes ago" / "2h ago" / "Yesterday" / "3 days ago" */
export function relativeAge(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const diff = Date.now() - t;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)} min ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  if (diff < 7 * 86_400_000) return `${Math.round(diff / 86_400_000)} days ago`;
  return formatDate(iso);
}

const SOURCE_LABELS: Record<string, string> = {
  FTS: "Find a Tender",
  CF: "Contracts Finder",
  PCS: "Public Contracts Scotland",
  S2W: "Sell2Wales",
  NI: "eTendersNI",
};

export function sourceLabel(code: string): string {
  return SOURCE_LABELS[code] ?? code;
}
