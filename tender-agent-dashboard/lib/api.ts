// Typed client for the Tender Agent backend.
// Every dashboard fetch goes through here — never call fetch() from a component.

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

// ---------------------------------------------------------------------------
// Types — mirror tender_agent/schemas.py. Keep field names/casing in sync.
// ---------------------------------------------------------------------------

export type SourceCode = "FTS" | "CF" | "PCS" | "S2W" | "NI";

export interface TenderDocumentRaw {
  title?: string | null;
  url: string;
  format?: string | null;
}

export interface Tender {
  id: number;
  source_code: string;
  source_ref: string;
  source_url: string | null;
  title: string;
  description: string | null;
  buyer_name: string | null;
  notice_type: string | null;
  status: string | null;
  cpv_codes: string[] | null;
  value_amount: string | number | null; // Decimal serialises as string from FastAPI
  value_currency: string | null;
  published_at: string | null;
  deadline_at: string | null;
  documents: TenderDocumentRaw[] | null;
  first_seen_at: string;
  last_seen_at: string;
}

export interface EvaluationCriterion {
  name: string;
  weight?: number | null;
  description?: string | null;
}

export interface RequirementItem {
  text: string;
  category?: string | null;
  evidence_required?: string | null;
  source_excerpt?: string | null;
  confidence?: "low" | "medium" | "high" | null;
}

export interface DocumentRequired {
  name: string;
  description?: string | null;
  mandatory?: boolean | null;
}

export interface QuestionToAnswer {
  question: string;
  word_limit?: number | null;
  weight?: number | null;
  section?: string | null;
}

export interface TenderRequirements {
  id: number;
  tender_id: number;
  summary: string | null;
  evaluation_criteria: EvaluationCriterion[] | null;
  mandatory_requirements: RequirementItem[] | null;
  desired_requirements: RequirementItem[] | null;
  documents_required: DocumentRequired[] | null;
  questions_to_answer: QuestionToAnswer[] | null;
  risk_flags: string[] | null;
  estimated_effort_days: string | number | null;
  recommendation: string | null;
  recommendation_reason: string | null;
  model: string | null;
  extracted_at: string;
}

export interface TenderDocumentFile {
  id: number;
  tender_id: number;
  url: string;
  title: string | null;
  format: string | null;
  bytes: number | null;
  sha256: string | null;
  download_status: string;
  error: string | null;
  downloaded_at: string | null;
}

export interface FilterProfileCreate {
  name: string;
  enabled?: boolean;
  cpv_codes?: string[] | null;
  cpv_prefixes?: string[] | null;
  keywords_any?: string[] | null;
  keywords_all?: string[] | null;
  keywords_none?: string[] | null;
  buyer_names?: string[] | null;
  regions?: string[] | null;
  countries?: string[] | null;
  notice_types?: string[] | null;
  value_min?: string | number | null;
  value_max?: string | number | null;
  min_days_to_deadline?: number | null;
}

export interface FilterProfile extends FilterProfileCreate {
  id: number;
  created_at: string;
  match_count: number;
  // Backend always populates this; narrow from the optional in FilterProfileCreate.
  enabled: boolean;
}

// Every field optional — used for PATCH /filters/{id} (e.g. toggling `enabled`).
export type FilterProfileUpdate = Partial<FilterProfileCreate>;

export interface ListTendersParams {
  source?: SourceCode | string;
  buyer?: string;
  status?: string;
  deadline_after?: string;
  matched_only?: boolean;
  include_duplicates?: boolean;
  limit?: number;
  offset?: number;
}

// ---------------------------------------------------------------------------
// Low-level fetcher
// ---------------------------------------------------------------------------

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail: unknown = null;
    try {
      detail = await res.json();
    } catch {
      // ignore
    }
    throw new ApiError(res.status, res.statusText, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, statusText: string, detail: unknown) {
    super(`API ${status} ${statusText}`);
    this.status = status;
    this.detail = detail;
  }
}

// SWR fetcher — pass paths starting with "/".
export const fetcher = <T = unknown>(path: string) => request<T>(path);

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export function tendersPath(params: ListTendersParams = {}): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    q.set(k, String(v));
  }
  const qs = q.toString();
  return `/tenders${qs ? `?${qs}` : ""}`;
}

export const listTenders = (params: ListTendersParams = {}) =>
  request<Tender[]>(tendersPath(params));

export const getTender = (id: number) => request<Tender>(`/tenders/${id}`);

export const getTenderRequirements = (id: number) =>
  request<TenderRequirements>(`/tenders/${id}/requirements`);

export const getTenderDocuments = (id: number) =>
  request<TenderDocumentFile[]>(`/tenders/${id}/documents`);

export const listFilters = () => request<FilterProfile[]>("/filters");

export const createFilter = (payload: FilterProfileCreate) =>
  request<FilterProfile>("/filters", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const replaceFilter = (id: number, payload: FilterProfileCreate) =>
  request<FilterProfile>(`/filters/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });

export const updateFilter = (id: number, payload: FilterProfileUpdate) =>
  request<FilterProfile>(`/filters/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });

export const deleteFilter = (id: number) =>
  request<void>(`/filters/${id}`, { method: "DELETE" });

export const triggerPollNow = () =>
  request<{ status: string }>("/admin/poll-now", { method: "POST" });

// ---------------------------------------------------------------------------
// View helpers (used by TenderCard and detail page)
// ---------------------------------------------------------------------------

export function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const ms = t - Date.now();
  return Math.ceil(ms / 86_400_000);
}

const CURRENCY_SYMBOL: Record<string, string> = {
  GBP: "£",
  EUR: "€",
  USD: "$",
};

export function formatValue(
  amount: string | number | null,
  currency: string | null,
): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(n)) return "—";
  const symbol = currency ? CURRENCY_SYMBOL[currency] ?? `${currency} ` : "";
  // Compact for readability: 1.2M, 450k, 12,000.
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${symbol}${(n / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `${symbol}${Math.round(n / 1_000)}k`;
  return `${symbol}${n.toLocaleString("en-GB")}`;
}
