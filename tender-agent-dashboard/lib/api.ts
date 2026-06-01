// Typed client for the Tender Agent backend.
// Every dashboard fetch goes through here — never call fetch() from a component.

// Browser-side calls go through the Next.js rewrite at /__api/* so they're
// same-origin (avoids backend CORS). Server-side renders / route handlers can
// override this with NEXT_PUBLIC_API_BASE_URL when the backend lives on a
// different origin than the dashboard process can reach via /__api.
const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "/__api";

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
  // Chunk 5: true when extracted text for this file's sha256 is stored at the
  // current extractor_version. Means the brief engine can re-use it without
  // re-reading the file (and a different tender with the same ITT can re-use
  // it without re-downloading).
  content_stored?: boolean;
}

// Chunk 5: bid-brief engine.

export type BriefRecommendation = "bid" | "no_bid" | "conditional";
export type BriefConfidence = "high" | "medium" | "low";
export type RiskSeverity = "high" | "medium" | "low";
export type BriefStatus = "generating" | "complete" | "failed";

export interface BriefKeyRisk {
  risk: string;
  severity: RiskSeverity;
  detail?: string | null;
}

export interface BriefDeadline {
  date?: string | null;
  note?: string | null;
}

export interface BriefContractValue {
  amount?: string | null;
  note?: string | null;
}

export interface BriefScoringCriterion {
  criterion: string;
  weight?: string | null;
}

export interface BriefScoring {
  summary?: string | null;
  criteria?: BriefScoringCriterion[];
}

export interface BriefJson {
  recommendation: BriefRecommendation;
  confidence: BriefConfidence;
  headline: string;
  rationale: string;
  key_risks: BriefKeyRisk[];
  deadline?: BriefDeadline | null;
  contract_value?: BriefContractValue | null;
  mandatory_requirements?: string[];
  scoring?: BriefScoring | null;
  scope_summary?: string | null;
  notable_conditions?: string[];
  missing_or_unclear?: string[];
}

export interface BriefDocumentConsidered {
  filename: string;
  included: "full" | "truncated" | "omitted";
  sha256?: string | null;
  doc_type?: string | null;
}

export interface TenderBrief {
  id: number;
  tender_id: number;
  status: BriefStatus;
  recommendation: BriefRecommendation | null;
  confidence: BriefConfidence | null;
  headline: string | null;
  brief_json: BriefJson | null;
  model: string | null;
  documents_considered: BriefDocumentConsidered[] | null;
  input_tokens: number | null;
  output_tokens: number | null;
  error_detail: string | null;
  generated_at: string | null;
  created_at: string;
  updated_at: string;
}

export const startGenerateBrief = (tenderId: number) =>
  request<TenderBrief>(`/tenders/${tenderId}/generate-brief`, {
    method: "POST",
  });

export const getLatestBrief = (tenderId: number) =>
  request<TenderBrief | null>(`/tenders/${tenderId}/brief`);

export function isBriefActive(status: string): boolean {
  return status === "generating";
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
// Tender search (Phase 4 chunk 7) — mirrors tender_agent/schemas.py
// (TenderSearchResult / TenderSearchResponse / TenderFacets) and the
// /tenders/search query params. Source-agnostic: a result from any source
// (current or future) carries the same fields.
// ---------------------------------------------------------------------------

export type SearchSort = "deadline_asc" | "published_desc" | "value_desc";

export interface TenderSearchResult {
  id: number;
  title: string;
  buyer_name: string | null;
  buyer_region: string | null;
  // Canonical UK region (chunk 8); buyer_region is the raw source locality.
  region: string | null;
  status: string | null;
  source_code: string;
  source_url: string | null;
  cpv_codes: string[] | null;
  value_amount: string | number | null;
  value_currency: string | null;
  value_min: string | number | null;
  value_max: string | number | null;
  published_at: string | null;
  deadline_at: string | null;
  procurement_ref: string | null;
  duplicate_of_id: number | null;
  is_duplicate: boolean;
}

export interface TenderSearchResponse {
  total: number;
  page: number;
  page_size: number;
  results: TenderSearchResult[];
}

// One canonical region present in the table, with its tender count (chunk 8).
export interface RegionFacet {
  value: string;
  count: number;
}

export interface TenderFacets {
  sources: string[];
  statuses: string[];
  regions: RegionFacet[];
}

export interface TenderSearchParams {
  q?: string;
  cpv?: string[];
  region?: string[];
  buyer?: string;
  value_min?: string | number | null;
  value_max?: string | number | null;
  // With a value range set, exclude null-value tenders (default: include them).
  value_stated_only?: boolean;
  deadline_from?: string;
  deadline_to?: string;
  open_only?: boolean;
  status?: string[];
  source?: string[];
  include_duplicates?: boolean;
  sort?: SearchSort;
  page?: number;
  page_size?: number;
}

// Builds /tenders/search?... — arrays become repeated params (cpv=a&cpv=b),
// empties/nulls are dropped so the URL stays a stable SWR cache key.
export function searchPath(params: TenderSearchParams): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item === undefined || item === null || item === "") continue;
        q.append(k, String(item));
      }
    } else {
      q.set(k, String(v));
    }
  }
  const qs = q.toString();
  return `/tenders/search${qs ? `?${qs}` : ""}`;
}

export const searchTenders = (params: TenderSearchParams) =>
  request<TenderSearchResponse>(searchPath(params));

export const getTenderFacets = () => request<TenderFacets>("/tenders/facets");

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

export interface FetchTask {
  task_id: string;
  tender_id: number;
  status: string;
  adapter: string | null;
  platform_slug: string | null;
  files_count: number;
  missing_count: number;
  detail: string | null;
  login_url: string | null;
  bridge_session_slug: string | null;
  waiting_since: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export const startFetchDocuments = (tenderId: number) =>
  request<FetchTask>(`/tenders/${tenderId}/fetch-documents`, { method: "POST" });

export const getFetchStatus = (tenderId: number, taskId: string) =>
  request<FetchTask>(`/tenders/${tenderId}/fetch-documents/${taskId}`);

export const confirmFetch = (tenderId: number, taskId: string) =>
  request<FetchTask>(
    `/tenders/${tenderId}/fetch-documents/${taskId}/confirm`,
    { method: "POST" },
  );

export const cancelFetch = (tenderId: number, taskId: string) =>
  request<FetchTask>(
    `/tenders/${tenderId}/fetch-documents/${taskId}/cancel`,
    { method: "POST" },
  );

export const getBridgeHealth = () =>
  request<{ available: boolean }>("/system/bridge-health");

// Direct link to a stored document's bytes (served by the backend with a
// sanitised download filename). Goes through the same /__api proxy.
export function documentFileUrl(tenderId: number, docId: number): string {
  return `${API_BASE}/tenders/${tenderId}/documents/${docId}/file`;
}

// A fetch task is still progressing on its own while queued/running, and while
// waiting for the user to log in (it auto-continues once they do). It pauses
// (awaiting an explicit user action) at needs_user_confirmation.
export function isFetchTaskActive(status: string): boolean {
  return (
    status === "queued" || status === "running" || status === "waiting_for_login"
  );
}

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
// Vault types + helpers
// ---------------------------------------------------------------------------

export type VaultCategory =
  | "corporate"
  | "financial"
  | "insurance"
  | "accreditation"
  | "policy"
  | "capability"
  | "people"
  | "technical"
  | "past_bid"
  | "uncategorised";

export type DocType =
  | "insurance_certificate"
  | "iso_certificate"
  | "policy"
  | "case_study"
  | "accounts";

export interface InsuranceClaims {
  doc_type: "insurance_certificate";
  insurance_type?:
    | "employers_liability"
    | "public_liability"
    | "professional_indemnity"
    | "product"
    | "cyber"
    | null;
  cover_amount?: string | number | null;
  currency?: string | null;
  insurer?: string | null;
  insurer_uk_authorised?: boolean | null;
  policy_holder?: string | null;
  policy_number?: string | null;
  valid_from?: string | null;
  valid_until?: string | null;
  territory?: string | null;
  low_confidence_fields?: string[];
  notes?: string[];
}

export interface IsoClaims {
  doc_type: "iso_certificate";
  standard?: string | null;
  standard_version?: string | null;
  scope?: string | null;
  certifying_body?: string | null;
  certificate_number?: string | null;
  issued_date?: string | null;
  valid_until?: string | null;
  holder?: string | null;
  low_confidence_fields?: string[];
  notes?: string[];
}

export interface PolicyClaims {
  doc_type: "policy";
  policy_kind?: string | null;
  title?: string | null;
  covers?: string[];
  references_standards?: string[];
  signed_by_director?: boolean | null;
  signatory_name?: string | null;
  signed_date?: string | null;
  review_due?: string | null;
  low_confidence_fields?: string[];
  notes?: string[];
}

export interface CaseStudyClaims {
  doc_type: "case_study";
  client?: string | null;
  client_sector?: string | null;
  client_anonymised?: boolean | null;
  value?: string | number | null;
  currency?: string | null;
  delivered_from?: string | null;
  delivered_to?: string | null;
  services?: string[];
  outcomes?: string[];
  team_size?: number | null;
  location?: string | null;
  consent_to_name_client?: boolean | null;
  low_confidence_fields?: string[];
  notes?: string[];
}

export interface AccountsClaims {
  doc_type: "accounts";
  fiscal_year_end?: string | null;
  turnover?: string | number | null;
  currency?: string | null;
  profit_before_tax?: string | number | null;
  audited?: boolean | null;
  auditor?: string | null;
  low_confidence_fields?: string[];
  notes?: string[];
}

export type ClaimsRecord =
  | InsuranceClaims
  | IsoClaims
  | PolicyClaims
  | CaseStudyClaims
  | AccountsClaims
  | { doc_type?: string; [k: string]: unknown };

export interface VaultDocumentVersion {
  id: number;
  document_id: number;
  version: number;
  storage_key: string;
  bytes: number;
  sha256: string;
  mime_type: string;
  title: string;
  expiry_date: string | null;
  issuing_body: string | null;
  issued_date: string | null;
  last_reviewed_date: string | null;
  superseded_by_version_id: number | null;
  claims: ClaimsRecord;
  claims_confirmed: boolean;
  text_extracted: string | null;
  uploaded_by: string | null;
  uploaded_at: string;
}

export interface VaultDocument {
  id: number;
  org_id: number;
  category: VaultCategory;
  subcategory: string | null;
  title: string;
  owner_email: string | null;
  confidentiality: string;
  created_at: string;
  current_version: VaultDocumentVersion | null;
}

export const listVaultDocuments = () =>
  request<VaultDocument[]>("/vault/documents");

export const getVaultDocument = (id: number) =>
  request<VaultDocument>(`/vault/documents/${id}`);

export const listExpiringVaultDocuments = (days: number = 30) =>
  request<VaultDocument[]>(`/vault/expiring?days=${days}`);

export const archiveVaultDocument = (id: number) =>
  request<void>(`/vault/documents/${id}`, { method: "DELETE" });

export interface VaultClaimsPatch {
  claims: ClaimsRecord;
  claims_confirmed?: boolean | null;
  expiry_date?: string | null;
  issuing_body?: string | null;
  issued_date?: string | null;
}

export const updateVaultClaims = (
  documentId: number,
  versionId: number,
  payload: VaultClaimsPatch,
) =>
  request<VaultDocument>(
    `/vault/documents/${documentId}/versions/${versionId}/claims`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );

// Multipart upload — fetch directly because the JSON `request` helper sets a
// JSON content-type. Browser sets the multipart boundary automatically.
export async function uploadVaultDocument(formData: FormData): Promise<VaultDocument> {
  const base = API_BASE;
  const res = await fetch(`${base}/vault/documents`, {
    method: "POST",
    body: formData,
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
  return (await res.json()) as VaultDocument;
}

export async function supersedeVaultDocument(
  documentId: number,
  formData: FormData,
): Promise<VaultDocument> {
  const base = API_BASE;
  const res = await fetch(`${base}/vault/documents/${documentId}/supersede`, {
    method: "POST",
    body: formData,
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
  return (await res.json()) as VaultDocument;
}

const VAULT_CATEGORY_LABELS: Record<VaultCategory, string> = {
  corporate: "Corporate",
  financial: "Financial",
  insurance: "Insurance",
  accreditation: "Accreditation",
  policy: "Policy",
  capability: "Capability",
  people: "People",
  technical: "Technical",
  past_bid: "Past bid",
  uncategorised: "Uncategorised",
};

export function vaultCategoryLabel(c: string): string {
  return VAULT_CATEGORY_LABELS[c as VaultCategory] ?? c;
}

export function isPlaceholderTitle(title: string): boolean {
  return title.startsWith("PLACEHOLDER -- ") || title.startsWith("PLACEHOLDER — ");
}

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

// ---------------------------------------------------------------------------
// Portal registry types + helpers
// ---------------------------------------------------------------------------

export type LoginType =
  | "none"
  | "email_only"
  | "username_password"
  | "2fa"
  | "oauth"
  | "unknown";

export type AdapterStatus =
  | "not_started"
  | "stub"
  | "read_only"
  | "full"
  | "deprecated";

export type Priority = "critical" | "high" | "medium" | "low";

export type ExtractedFrom =
  | "description"
  | "additional_information"
  | "documents"
  | "contact"
  | "parties";

export type SightingType =
  | "tender_link"
  | "document_link"
  | "contact_email"
  | "reference_text";

export interface Portal {
  id: number;
  domain: string;
  display_name: string;
  url_patterns: string[];
  login_type: LoginType;
  adapter_status: AdapterStatus;
  adapter_module: string | null;
  priority: Priority;
  tender_count: number;
  first_seen_at: string;
  last_seen_at: string;
  classification_data: Record<string, unknown> | null;
  notes: string | null;
  platform_id: number | null;
  is_email_domain: boolean;
  created_at: string;
  updated_at: string;
}

export interface PortalSighting {
  id: number;
  portal_id: number | null;
  tender_id: number;
  url: string;
  extracted_from: ExtractedFrom;
  sighting_type: SightingType;
  extracted_at: string;
  tender_title: string | null;
}

export interface PortalDetail extends Portal {
  recent_sightings: PortalSighting[];
}

export interface ListPortalsParams {
  adapter_status?: AdapterStatus | "";
  priority?: Priority | "";
  search?: string;
  has_login_type?: LoginType | "";
  platform_id?: number;
  include_email_domains?: boolean;
  sort?: "tender_count" | "last_seen" | "first_seen" | "priority" | "";
  limit?: number;
  offset?: number;
}

export type PlatformKind = "transactional" | "special";

export interface PortalPlatform {
  id: number;
  slug: string;
  vendor: string;
  display_name: string;
  domain_patterns: string[];
  kind: PlatformKind;
  adapter_status: AdapterStatus;
  adapter_module: string | null;
  login_type: LoginType;
  notes: string | null;
  created_at: string;
  updated_at: string;
  total_tender_count: number;
  buyer_instance_count: number;
}

export interface PlatformPortalRow {
  id: number;
  domain: string;
  display_name: string;
  tender_count: number;
  adapter_status: AdapterStatus;
}

export interface PortalPlatformDetail extends PortalPlatform {
  portals: PlatformPortalRow[];
  sample_tender_urls: string[];
}

export interface PlatformUpdate {
  adapter_status?: AdapterStatus;
  adapter_module?: string | null;
  login_type?: LoginType;
  notes?: string | null;
}

export interface PortalUpdate {
  display_name?: string;
  login_type?: LoginType;
  priority?: Priority;
  adapter_status?: AdapterStatus;
  adapter_module?: string | null;
  notes?: string | null;
}

export interface BlocklistEntry {
  id: number;
  domain: string;
  reason: string | null;
  added_at: string;
  added_by: string;
}

export function portalsPath(params: ListPortalsParams = {}): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    q.set(k, String(v));
  }
  const qs = q.toString();
  return `/portals${qs ? `?${qs}` : ""}`;
}

export const listPortals = (params: ListPortalsParams = {}) =>
  request<Portal[]>(portalsPath(params));

export const getPortal = (id: number) => request<PortalDetail>(`/portals/${id}`);

export const updatePortal = (id: number, body: PortalUpdate) =>
  request<Portal>(`/portals/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deprecatePortal = (id: number) =>
  request<Portal>(`/portals/${id}`, { method: "DELETE" });

export const reclassifyPortal = (id: number) =>
  request<{ portal_id: number; status: string; queued: boolean }>(
    `/portals/${id}/classify`,
    { method: "POST" },
  );

export const listSightings = (portalId: number, offset = 0, limit = 50) =>
  request<PortalSighting[]>(
    `/portals/${portalId}/sightings?offset=${offset}&limit=${limit}`,
  );

export const listBlocklist = () =>
  request<BlocklistEntry[]>("/portal-blocklist");

export const addBlocklistDomain = (body: { domain: string; reason?: string }) =>
  request<BlocklistEntry>("/portal-blocklist", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const removeBlocklistDomain = (id: number) =>
  request<void>(`/portal-blocklist/${id}`, { method: "DELETE" });

// --- Platforms ---

export const listPlatforms = () => request<PortalPlatform[]>("/platforms");

export const getPlatform = (slug: string) =>
  request<PortalPlatformDetail>(`/platforms/${slug}`);

export const updatePlatform = (slug: string, body: PlatformUpdate) =>
  request<PortalPlatform>(`/platforms/${slug}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

const ADAPTER_STATUS_LABELS: Record<AdapterStatus, string> = {
  not_started: "Not started",
  stub: "Stub",
  read_only: "Read-only",
  full: "Full",
  deprecated: "Deprecated",
};

export function adapterStatusLabel(s: AdapterStatus): string {
  return ADAPTER_STATUS_LABELS[s] ?? s;
}

const LOGIN_TYPE_LABELS: Record<LoginType, string> = {
  none: "None",
  email_only: "Email only",
  username_password: "Username + password",
  "2fa": "Two-factor",
  oauth: "OAuth",
  unknown: "Unknown",
};

export function loginTypeLabel(s: LoginType): string {
  return LOGIN_TYPE_LABELS[s] ?? s;
}

const PRIORITY_LABELS: Record<Priority, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

export function priorityLabel(p: Priority): string {
  return PRIORITY_LABELS[p] ?? p;
}

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

// ---------------------------------------------------------------------------
// Chunk 6 — accounts, brief gate, Stripe billing.
// ---------------------------------------------------------------------------

export type PlanCode = "free" | "payg" | "plan_100" | "plan_unlimited";

export interface Me {
  id: number;
  email: string;
  plan: PlanCode;
  plan_limit: number | null;
  plan_used: number;
  is_plan_active: boolean;
  has_unlimited: boolean;
}

export interface BillingStatus {
  payments_configured: boolean;
  publishable_key: string | null;
  plan_prices: Record<string, string>;
}

export interface SubmissionFeeQuote {
  tender_id: number;
  amount_pence: number;
  amount_gbp: number;
  currency: string;
  payments_configured: boolean;
  already_paid: boolean;
}

export interface CheckoutResponse {
  status: "ok" | "payments_not_configured";
  url?: string | null;
  session_id?: string | null;
  amount_pence?: number | null;
  message?: string | null;
}

/** Anonymous-friendly. Returns null when not logged in. */
export async function fetchMe(): Promise<Me | null> {
  const res = await fetch(`${API_BASE}/me`, { credentials: "include" });
  if (!res.ok) return null;
  return (await res.json()) as Me | null;
}

export async function signup(
  email: string,
  password: string,
): Promise<Me> {
  const res = await fetch(`${API_BASE}/accounts/signup`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as Me;
}

export async function login(email: string, password: string): Promise<Me> {
  const res = await fetch(`${API_BASE}/accounts/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as Me;
}

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/accounts/logout`, {
    method: "POST",
    credentials: "include",
  });
}

export async function freeAlertsSignup(
  email: string,
  password: string,
): Promise<{ message: string }> {
  const res = await fetch(`${API_BASE}/accounts/free-alerts`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as { message: string };
}

export async function fetchBillingStatus(): Promise<BillingStatus> {
  const res = await fetch(`${API_BASE}/billing/status`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as BillingStatus;
}

export async function fetchSubmissionFee(
  tenderId: number,
): Promise<SubmissionFeeQuote | null> {
  const res = await fetch(`${API_BASE}/billing/submission-fee/${tenderId}`, {
    credentials: "include",
  });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as SubmissionFeeQuote;
}

export async function startCheckout(
  kind: "payg_brief" | "plan_100" | "plan_unlimited" | "submission_package",
  tenderId?: number,
): Promise<CheckoutResponse> {
  const res = await fetch(`${API_BASE}/billing/checkout`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind,
      tender_id: tenderId,
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as CheckoutResponse;
}

export interface DocumentContent {
  doc_id: number;
  tender_id: number;
  title: string | null;
  format: string | null;
  locked: boolean;
  char_count: number;
  preview_chars: number;
  text: string;
  unlock?: { reason: string; message: string };
}

export async function fetchDocumentContent(
  tenderId: number,
  docId: number,
): Promise<DocumentContent> {
  const res = await fetch(
    `${API_BASE}/tenders/${tenderId}/documents/${docId}/content`,
    { credentials: "include" },
  );
  if (!res.ok) throw new Error(await res.text());
  return (await res.json()) as DocumentContent;
}
