// Render a vault document's claims record per its discriminator. Each
// concrete type (insurance / iso / policy / case_study / accounts) gets a
// tailored layout — never a generic JSON dump.

import {
  type AccountsClaims,
  type CaseStudyClaims,
  type ClaimsRecord,
  type InsuranceClaims,
  type IsoClaims,
  type PolicyClaims,
} from "@/lib/api";
import { formatCurrency, formatDate } from "@/lib/format";

export default function ClaimsDisplay({ claims }: { claims: ClaimsRecord }) {
  const dt = (claims as { doc_type?: string }).doc_type;
  if (dt === "insurance_certificate") return <InsuranceClaimsView claims={claims as InsuranceClaims} />;
  if (dt === "iso_certificate") return <IsoClaimsView claims={claims as IsoClaims} />;
  if (dt === "policy") return <PolicyClaimsView claims={claims as PolicyClaims} />;
  if (dt === "case_study") return <CaseStudyClaimsView claims={claims as CaseStudyClaims} />;
  if (dt === "accounts") return <AccountsClaimsView claims={claims as AccountsClaims} />;
  return <UnknownClaimsView claims={claims} />;
}

// ---------------------------------------------------------------------------
// Shared row layout
// ---------------------------------------------------------------------------

function ClaimRow({
  label,
  children,
  flagged = false,
}: {
  label: string;
  children: React.ReactNode;
  flagged?: boolean;
}) {
  return (
    <div className="grid grid-cols-1 gap-1 border-b border-border py-3 last:border-b-0 md:grid-cols-[200px_1fr] md:gap-6 md:py-4">
      <dt
        className="text-text-dim"
        style={{
          fontSize: "11px",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          fontWeight: 500,
        }}
      >
        {label}
        {flagged && (
          <span className="ml-2 text-amber" title="Low confidence">
            ⚠
          </span>
        )}
      </dt>
      <dd className="text-text" style={{ fontSize: "15px", lineHeight: 1.5 }}>
        {children}
      </dd>
    </div>
  );
}

function MonoValue({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="font-mono"
      style={{
        fontSize: "13px",
        padding: "3px 8px",
        background: "rgba(255, 255, 255, 0.04)",
        border: "1px solid rgba(255, 255, 255, 0.08)",
        borderRadius: "4px",
        letterSpacing: "0.02em",
      }}
    >
      {children}
    </span>
  );
}

function PlainValue({ value }: { value: string | number | boolean | null | undefined }) {
  if (value === null || value === undefined || value === "") {
    return <span className="italic text-text-dim">Not stated</span>;
  }
  if (typeof value === "boolean") {
    return (
      <span className={value ? "text-mint-pale" : "text-text-muted"}>
        {value ? "Yes" : "No"}
      </span>
    );
  }
  return <>{value}</>;
}

function NoteBlock({ notes }: { notes?: string[] }) {
  if (!notes || notes.length === 0) return null;
  return (
    <div
      className="rounded-xl border border-border bg-bg-elevated p-5 backdrop-blur-sm"
      style={{ borderRadius: "12px" }}
    >
      <div
        className="text-text-dim mb-3"
        style={{
          fontSize: "11px",
          letterSpacing: "0.14em",
          textTransform: "uppercase",
          fontWeight: 500,
        }}
      >
        Extractor notes
      </div>
      <ul className="space-y-1.5">
        {notes.map((note, i) => (
          <li
            key={i}
            className="text-text-muted"
            style={{ fontSize: "13px", lineHeight: 1.55 }}
          >
            · {note}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Insurance certificate
// ---------------------------------------------------------------------------

function InsuranceClaimsView({ claims }: { claims: InsuranceClaims }) {
  const low = new Set(claims.low_confidence_fields ?? []);
  const cover = formatCurrency(claims.cover_amount ?? null, claims.currency ?? "GBP");
  const insuranceTypeLabels: Record<string, string> = {
    employers_liability: "Employers liability",
    public_liability: "Public liability",
    professional_indemnity: "Professional indemnity",
    product: "Product liability",
    cyber: "Cyber",
  };
  return (
    <div className="space-y-6">
      <dl className="rounded-xl border border-border bg-bg-elevated p-2 px-6 backdrop-blur-sm" style={{ borderRadius: "12px" }}>
        <ClaimRow label="Insurance type" flagged={low.has("insurance_type")}>
          {claims.insurance_type ? (
            <PlainValue value={insuranceTypeLabels[claims.insurance_type] ?? claims.insurance_type} />
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Cover amount" flagged={low.has("cover_amount")}>
          {cover ? (
            <span className="display-num text-mint-pale" style={{ fontSize: "22px", lineHeight: 1 }}>
              {cover}
            </span>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Insurer" flagged={low.has("insurer")}>
          <div className="flex flex-wrap items-center gap-2">
            <PlainValue value={claims.insurer} />
            {claims.insurer_uk_authorised && (
              <span
                className="text-mint-pale"
                style={{
                  fontSize: "10px",
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  padding: "2px 7px",
                  border: "1px solid rgba(159, 242, 178, 0.4)",
                  borderRadius: "4px",
                }}
              >
                UK authorised
              </span>
            )}
          </div>
        </ClaimRow>
        <ClaimRow label="Policy holder" flagged={low.has("policy_holder")}>
          <PlainValue value={claims.policy_holder} />
        </ClaimRow>
        <ClaimRow label="Policy number" flagged={low.has("policy_number")}>
          {claims.policy_number ? <MonoValue>{claims.policy_number}</MonoValue> : <PlainValue value={null} />}
        </ClaimRow>
        <ClaimRow label="Valid from" flagged={low.has("valid_from")}>
          <PlainValue value={formatDate(claims.valid_from ?? null)} />
        </ClaimRow>
        <ClaimRow label="Valid until" flagged={low.has("valid_until")}>
          <PlainValue value={formatDate(claims.valid_until ?? null)} />
        </ClaimRow>
        <ClaimRow label="Territory" flagged={low.has("territory")}>
          <PlainValue value={claims.territory} />
        </ClaimRow>
      </dl>
      <NoteBlock notes={claims.notes} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ISO certificate
// ---------------------------------------------------------------------------

function IsoClaimsView({ claims }: { claims: IsoClaims }) {
  const low = new Set(claims.low_confidence_fields ?? []);
  return (
    <div className="space-y-6">
      <dl className="rounded-xl border border-border bg-bg-elevated p-2 px-6 backdrop-blur-sm" style={{ borderRadius: "12px" }}>
        <ClaimRow label="Standard" flagged={low.has("standard")}>
          {claims.standard ? (
            <span className="display-num text-text" style={{ fontSize: "22px", lineHeight: 1 }}>
              {claims.standard}
              {claims.standard_version && (
                <span className="ml-2 text-text-muted" style={{ fontSize: "14px" }}>
                  ({claims.standard_version})
                </span>
              )}
            </span>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Scope" flagged={low.has("scope")}>
          <PlainValue value={claims.scope} />
        </ClaimRow>
        <ClaimRow label="Certifying body" flagged={low.has("certifying_body")}>
          <PlainValue value={claims.certifying_body} />
        </ClaimRow>
        <ClaimRow label="Certificate number" flagged={low.has("certificate_number")}>
          {claims.certificate_number ? <MonoValue>{claims.certificate_number}</MonoValue> : <PlainValue value={null} />}
        </ClaimRow>
        <ClaimRow label="Holder" flagged={low.has("holder")}>
          <PlainValue value={claims.holder} />
        </ClaimRow>
        <ClaimRow label="Issued" flagged={low.has("issued_date")}>
          <PlainValue value={formatDate(claims.issued_date ?? null)} />
        </ClaimRow>
        <ClaimRow label="Valid until" flagged={low.has("valid_until")}>
          <PlainValue value={formatDate(claims.valid_until ?? null)} />
        </ClaimRow>
      </dl>
      <NoteBlock notes={claims.notes} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Policy
// ---------------------------------------------------------------------------

function PolicyClaimsView({ claims }: { claims: PolicyClaims }) {
  const low = new Set(claims.low_confidence_fields ?? []);
  return (
    <div className="space-y-6">
      <dl className="rounded-xl border border-border bg-bg-elevated p-2 px-6 backdrop-blur-sm" style={{ borderRadius: "12px" }}>
        <ClaimRow label="Policy kind" flagged={low.has("policy_kind")}>
          <PlainValue value={claims.policy_kind?.replaceAll("_", " ")} />
        </ClaimRow>
        <ClaimRow label="Title" flagged={low.has("title")}>
          <PlainValue value={claims.title} />
        </ClaimRow>
        <ClaimRow label="Covers" flagged={low.has("covers")}>
          {claims.covers && claims.covers.length > 0 ? (
            <ul className="space-y-1">
              {claims.covers.map((c, i) => (
                <li key={i}>· {c}</li>
              ))}
            </ul>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="References" flagged={low.has("references_standards")}>
          {claims.references_standards && claims.references_standards.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {claims.references_standards.map((s) => (
                <MonoValue key={s}>{s}</MonoValue>
              ))}
            </div>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Signed by director" flagged={low.has("signed_by_director")}>
          <PlainValue value={claims.signed_by_director} />
        </ClaimRow>
        <ClaimRow label="Signatory" flagged={low.has("signatory_name")}>
          <PlainValue value={claims.signatory_name} />
        </ClaimRow>
        <ClaimRow label="Signed on" flagged={low.has("signed_date")}>
          <PlainValue value={formatDate(claims.signed_date ?? null)} />
        </ClaimRow>
        <ClaimRow label="Review due" flagged={low.has("review_due")}>
          <PlainValue value={formatDate(claims.review_due ?? null)} />
        </ClaimRow>
      </dl>
      <NoteBlock notes={claims.notes} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Case study
// ---------------------------------------------------------------------------

function CaseStudyClaimsView({ claims }: { claims: CaseStudyClaims }) {
  const low = new Set(claims.low_confidence_fields ?? []);
  const val = formatCurrency(claims.value ?? null, claims.currency ?? "GBP");
  return (
    <div className="space-y-6">
      <dl className="rounded-xl border border-border bg-bg-elevated p-2 px-6 backdrop-blur-sm" style={{ borderRadius: "12px" }}>
        <ClaimRow label="Client" flagged={low.has("client")}>
          <div className="flex flex-wrap items-center gap-2">
            <PlainValue value={claims.client} />
            {claims.client_anonymised && (
              <span
                className="text-text-dim"
                style={{
                  fontSize: "10px",
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  padding: "2px 7px",
                  border: "1px solid rgba(255, 255, 255, 0.16)",
                  borderRadius: "4px",
                }}
              >
                Anonymised
              </span>
            )}
          </div>
        </ClaimRow>
        <ClaimRow label="Sector" flagged={low.has("client_sector")}>
          <PlainValue value={claims.client_sector?.replaceAll("_", " ")} />
        </ClaimRow>
        <ClaimRow label="Contract value" flagged={low.has("value")}>
          {val ? (
            <span className="display-num text-mint-pale" style={{ fontSize: "22px", lineHeight: 1 }}>
              {val}
            </span>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Delivered" flagged={low.has("delivered_from")}>
          {claims.delivered_from || claims.delivered_to ? (
            <>
              {formatDate(claims.delivered_from ?? null) ?? "?"} →{" "}
              {formatDate(claims.delivered_to ?? null) ?? "?"}
            </>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Services" flagged={low.has("services")}>
          {claims.services && claims.services.length > 0 ? (
            <ul className="space-y-1">
              {claims.services.map((s, i) => (
                <li key={i}>· {s}</li>
              ))}
            </ul>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Outcomes" flagged={low.has("outcomes")}>
          {claims.outcomes && claims.outcomes.length > 0 ? (
            <ul className="space-y-1">
              {claims.outcomes.map((o, i) => (
                <li key={i}>· {o}</li>
              ))}
            </ul>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Team size" flagged={low.has("team_size")}>
          <PlainValue value={claims.team_size} />
        </ClaimRow>
        <ClaimRow label="Location" flagged={low.has("location")}>
          <PlainValue value={claims.location} />
        </ClaimRow>
        <ClaimRow label="Consent to name client" flagged={low.has("consent_to_name_client")}>
          <PlainValue value={claims.consent_to_name_client} />
        </ClaimRow>
      </dl>
      <NoteBlock notes={claims.notes} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------

function AccountsClaimsView({ claims }: { claims: AccountsClaims }) {
  const low = new Set(claims.low_confidence_fields ?? []);
  const turnover = formatCurrency(claims.turnover ?? null, claims.currency ?? "GBP");
  const profit = formatCurrency(claims.profit_before_tax ?? null, claims.currency ?? "GBP");
  return (
    <div className="space-y-6">
      <dl className="rounded-xl border border-border bg-bg-elevated p-2 px-6 backdrop-blur-sm" style={{ borderRadius: "12px" }}>
        <ClaimRow label="Fiscal year end" flagged={low.has("fiscal_year_end")}>
          <PlainValue value={formatDate(claims.fiscal_year_end ?? null)} />
        </ClaimRow>
        <ClaimRow label="Turnover" flagged={low.has("turnover")}>
          {turnover ? (
            <span className="display-num text-mint-pale" style={{ fontSize: "22px", lineHeight: 1 }}>
              {turnover}
            </span>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Profit before tax" flagged={low.has("profit_before_tax")}>
          {profit ? (
            <span className="display-num text-text" style={{ fontSize: "20px", lineHeight: 1 }}>
              {profit}
            </span>
          ) : (
            <PlainValue value={null} />
          )}
        </ClaimRow>
        <ClaimRow label="Audited" flagged={low.has("audited")}>
          <PlainValue value={claims.audited} />
        </ClaimRow>
        <ClaimRow label="Auditor" flagged={low.has("auditor")}>
          <PlainValue value={claims.auditor} />
        </ClaimRow>
      </dl>
      <NoteBlock notes={claims.notes} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unknown / unclassified
// ---------------------------------------------------------------------------

function UnknownClaimsView({ claims }: { claims: ClaimsRecord }) {
  const entries = Object.entries(claims as Record<string, unknown>).filter(
    ([k]) => k !== "low_confidence_fields" && k !== "notes",
  );
  return (
    <div className="space-y-6">
      <div
        role="note"
        className="rounded-xl border border-amber/40 bg-amber/5 p-5"
        style={{ borderRadius: "12px" }}
      >
        <div
          className="text-amber mb-2"
          style={{
            fontSize: "11px",
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            fontWeight: 500,
          }}
        >
          ⚠ Document type not classified
        </div>
        <p
          className="text-text-muted"
          style={{ fontSize: "13px", lineHeight: 1.55 }}
        >
          The claims for this document don&apos;t carry a recognised{" "}
          <code className="font-mono text-text">doc_type</code>. Showing
          the raw JSON below — edit the document to set its type.
        </p>
      </div>
      {entries.length > 0 ? (
        <dl className="rounded-xl border border-border bg-bg-elevated p-2 px-6 backdrop-blur-sm" style={{ borderRadius: "12px" }}>
          {entries.map(([k, v]) => (
            <ClaimRow key={k} label={k}>
              <MonoValue>{JSON.stringify(v)}</MonoValue>
            </ClaimRow>
          ))}
        </dl>
      ) : (
        <p className="italic text-text-dim" style={{ fontSize: "14px" }}>
          No claims data yet.
        </p>
      )}
    </div>
  );
}
