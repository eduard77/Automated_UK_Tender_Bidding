# Tender Agent — Project Specification

This document is the single source of truth for the **Tender Agent** project: a UK
public-sector tender discovery and bid-automation system. It consolidates the design
decisions already made, defines components that haven't been built yet, and lays out
the build plan with acceptance criteria for each phase.

If you (human or AI assistant) are about to make a design decision that contradicts
this document, **stop and surface the conflict** before coding. If something genuinely
needs to change, change this document first.

---

## 1. Purpose and scope

The agent automates the lifecycle of UK public tender bidding:

1. **Discover** new tenders matching the company's filter criteria across all major UK
   tender sources.
2. **Analyse** each matching tender's documents and produce a structured requirements
   brief with a go/no-go recommendation.
3. **Match** requirements against the company's document vault and surface a compliance
   matrix (met / partial / missing / expired).
4. **Help draft** missing documents (policies, case studies, method statements,
   capability statements).
5. **Apply** via the buyer's portal: register if needed (human-approved), download
   documents, draft responses, attach matched documents, build the final bid pack.
6. **Submit** — but only after a mandatory human review and approval click.
7. **Monitor** amendments, clarifications, and award notifications via email and portal
   polling, and keep the bid record up to date.

The agent is **decision-support and execution-assistance**, not a fully autonomous
bidder. Final submission is always human-gated. See §7 for the full checkpoint policy.

---

## 2. Architecture overview

```
                    ┌──────────────────────────────────────────────┐
                    │              Web Dashboard (PWA)             │
                    │  Next.js · push notifications · review UI    │
                    └──────────────────────────────────────────────┘
                                         │ REST
                    ┌──────────────────────────────────────────────┐
                    │            FastAPI Application               │
                    │   /tenders /filters /vault /bids /admin      │
                    └──────────────────────────────────────────────┘
                                         │
        ┌──────────────┬─────────────────┼─────────────────┬──────────────┐
        ▼              ▼                 ▼                 ▼              ▼
  ┌──────────┐  ┌────────────┐  ┌───────────────┐  ┌────────────┐  ┌──────────┐
  │Discovery │  │ Documents  │  │ Requirements  │  │   Vault    │  │  Bids /  │
  │ adapters │  │ downloader │  │ extractor     │  │  service   │  │ portals  │
  │ (FTS, CF,│  │ + extractor│  │ (Claude API)  │  │  + claims  │  │ (Play-   │
  │ PCS, S2W,│  │            │  │               │  │  matching  │  │ wright)  │
  │ NI)      │  │            │  │               │  │            │  │          │
  └──────────┘  └────────────┘  └───────────────┘  └────────────┘  └──────────┘
        │              │                 │                 │              │
        └──────────────┴─────────────────┼─────────────────┴──────────────┘
                                         ▼
                              ┌────────────────────┐
                              │     Postgres       │
                              │     S3 (docs)      │
                              │  Temporal (long-   │
                              │  running workflows)│
                              └────────────────────┘
                                         ▲
                              ┌──────────┴──────────┐
                              │  Email / Gmail API  │
                              │  Microsoft Graph    │
                              └─────────────────────┘
```

**Stack**

- **Language**: Python 3.12 (backend), TypeScript 5 (frontend)
- **Backend framework**: FastAPI + SQLAlchemy 2 (async-capable, sync sessions for now)
- **Frontend framework**: Next.js 15 (App Router), Tailwind CSS
- **Database**: PostgreSQL 16
- **Object storage**: S3 (UK/EU region) for documents and vault artefacts
- **Workflow engine**: Temporal (introduced in Phase 6)
- **Vector store**: pgvector (lives in main Postgres; no separate service)
- **Browser automation**: Playwright (Chromium) for portal adapters
- **LLM**: Anthropic Claude (Sonnet for drafting, Haiku for classification/extraction)
- **Secrets**: AWS Secrets Manager for portal credentials, OAuth tokens, API keys
- **Deploy**: AWS — ECS Fargate (app + worker), RDS Postgres, S3, Secrets Manager,
  CloudFront in front of the dashboard

**Why these choices**

- FastAPI + Pydantic gives strong typing across API boundaries and clean OpenAPI docs.
- Postgres + pgvector avoids a separate vector DB and keeps the operational surface
  small.
- Temporal is essential once bids span weeks with reminders, retries, and human-task
  pauses — celery and cron break down at that point.
- Playwright over Selenium because portal pages are JS-heavy and Playwright's
  auto-waiting is more reliable.

---

## 3. Build phases

Each phase has acceptance criteria. A phase is complete only when every criterion is
demonstrably met.

### Phase 1 — Discovery service core ✅ DONE (Pass 1)

Adapter framework, OCDS normaliser, deduplicator, filter engine, scheduler, ingestion
pipeline, REST API, Postgres schema, migrations, Docker compose, tests.

**Acceptance**: ✓ All 10 unit tests passing. ✓ FTS and CF adapters present. ✓ Ingestion
runs and persists. ✓ Filter matches recorded. ✓ Lint clean.

### Phase 2 — All UK sources + document enrichment ⏳ IN PROGRESS (Pass 2)

PCS, Sell2Wales, eTendersNI adapters. Document downloader with PDF/DOCX text
extraction. Claude-powered requirements extractor. Dashboard PWA with push
notifications. AWS deploy config.

**Acceptance**:
- All 5 source adapters registered and ingest cleanly from live endpoints.
- Document downloader pulls every URL in `tender.documents`, stores locally (Pass 2)
  or to S3 (later), and extracts text from PDFs and DOCX.
- Requirements extractor produces valid JSON matching the schema in §5.3 for at least
  10 real tenders, with no hallucinated fields.
- Dashboard installable as PWA on iOS Safari and Chrome Android, receives push
  notifications when a tender matches a filter.
- `docker compose up` brings the whole stack to life locally; a documented
  `terraform apply` (or CDK) brings it up on AWS.

**Status**: Backend done. Dashboard scaffold built but pages incomplete. AWS infra
not yet written.

### Phase 3 — Document vault

Self-populating vault, claims-record extraction per document type, per-tender
re-validation, gap reporting, document drafting workflows.

**Acceptance**:
- Vault schema in §5.4 implemented; CRUD via API.
- Manual upload and email-ingest both ingest documents, auto-classify, and extract
  claims records via Claude.
- Expiry tracking with 60/30/7-day alerts and blocking of expired docs from bids.
- Re-validation engine produces a compliance matrix against any tender's
  `documents_required`.
- Drafting workflows for policies, case studies, method statements, and capability
  statements: interview flow → Claude draft → human review → vault ingest with claims.
- Version pinning: a bid records the exact document version submitted.

### Phase 4 — First portal adapter

Build the Playwright adapter for **one** portal end-to-end. Choose the portal the
company bids through most often. Establish the pattern other portals will follow.

**Acceptance**:
- Portal adapter implements the full interface in §6.
- Credentials in AWS Secrets Manager; never in code or DB plaintext.
- Registration flow drafts the form and pauses for human approval before submission.
- Document download works; staged in the bid workspace.
- Amendment polling detects changes to a watched tender.
- **Submission is never automated**: the adapter prepares everything and the human
  clicks Submit in the dashboard.

### Phase 5 — Drafting agent

Vault-grounded response drafting. Reads ITT + vault, produces draft answers per
question, every claim linked to a vault source.

**Acceptance**:
- For every question in `tender_requirements.questions_to_answer`, the agent produces
  a draft response that fits the word limit.
- Every factual claim cites a vault document by ID; no uncited claims.
- Confidence flags: claims the agent isn't sure of are tagged for human review.
- The agent uses Claude Sonnet for drafting and Haiku for triage/classification.
- Case study generator: produces Typst-rendered case studies from notes + photos with
  brand consistency. Templates exist for one-pager, two-pager, four-pager.

### Phase 6 — Email, workflows, notifications

Gmail/Graph integration. Temporal workflows for long-running bids. Slack and dashboard
notifications for human-action points.

**Acceptance**:
- Inbound email parsing recognises clarifications, amendments, awards, and routes
  them to the right tender record.
- Outbound replies to buyers are **drafted by the agent, approved by a human, sent by
  the agent**. Never sent autonomously.
- Temporal workflow `BidWorkflow` runs from tender match → submission with explicit
  human-task signals at each checkpoint.
- Deadline reminders fire at T-14d, T-7d, T-3d, T-1d.

### Phase 7 — Hardening, additional portals, observability

Sentry, structured logs to CloudWatch, audit log queryable from dashboard, SOC-style
event review, additional portal adapters, performance and cost tuning.

---

## 4. Source code layout

```
tender-agent/                      Backend (Python)
├── src/tender_agent/
│   ├── adapters/                  Tender source adapters (one file per source)
│   │   ├── base.py
│   │   ├── fts.py
│   │   ├── contracts_finder.py
│   │   ├── pcs.py
│   │   ├── sell2wales.py
│   │   └── etendersni.py
│   ├── api/                       FastAPI routers (one file per resource)
│   ├── services/                  Business logic
│   │   ├── normaliser.py
│   │   ├── deduplicator.py
│   │   ├── filter_engine.py
│   │   ├── document_downloader.py
│   │   ├── requirements_extractor.py
│   │   ├── ingestion.py
│   │   ├── vault/                 (Phase 3) claims extraction, matching
│   │   ├── drafting/              (Phase 5) draft generators
│   │   └── portals/               (Phase 4) Playwright portal adapters
│   ├── workflows/                 (Phase 6) Temporal workflow definitions
│   ├── config.py
│   ├── db.py
│   ├── models.py                  All SQLAlchemy ORM models
│   ├── schemas.py                 All Pydantic models
│   ├── scheduler.py
│   └── main.py
├── alembic/
├── tests/
├── pyproject.toml
└── docker-compose.yml

tender-agent-dashboard/             Frontend (Next.js)
├── app/
│   ├── (root)/                    Tender list home
│   ├── tenders/[id]/              Tender detail + brief + compliance matrix
│   ├── filters/                   Filter profile CRUD
│   ├── vault/                     (Phase 3) document vault browser
│   ├── bids/[id]/                 (Phase 4+) bid workspace
│   └── api/                       Next.js API routes (push subscribe, etc.)
├── components/
├── lib/
└── public/

terraform/                          Infrastructure (Phase 2 deliverable)
├── modules/
└── envs/
    ├── staging/
    └── prod/
```

---

## 5. Component specifications

### 5.1 Discovery service

Already implemented. See `src/tender_agent/services/ingestion.py` and `adapters/`.

**Add a new source**: subclass `SourceAdapter`, implement `fetch_since`, register
in `adapters/__init__.py`. The OCDS normaliser handles any source that publishes OCDS.
For non-OCDS sources, write a source-specific converter in `services/normaliser.py`.

### 5.2 Document downloader

Implemented in `services/document_downloader.py`. Downloads tender attachments,
extracts text from PDF (pypdf) and DOCX (python-docx). Storage today is local
filesystem under `DOCUMENT_STORAGE_DIR`; **Phase 3** moves this to S3 with the same
interface.

Storage layout (current): `{DOCUMENT_STORAGE_DIR}/{tender_id}/{sha256[:2]}/{sha256}.{ext}`.
S3 layout (Phase 3): `s3://{bucket}/tenders/{tender_id}/{sha256}.{ext}`.

### 5.3 Requirements extractor

Implemented in `services/requirements_extractor.py`. Output schema (this is the
contract — anything written downstream depends on it):

```json
{
  "summary": "string (2-4 sentences)",
  "evaluation_criteria": [
    { "criterion": "string", "weight_pct": "number|null", "notes": "string|null" }
  ],
  "mandatory_requirements": [
    {
      "id": "M1",
      "requirement": "string",
      "evidence_needed": "string|null",
      "confidence": "high|medium|low"
    }
  ],
  "desired_requirements": [
    { "id": "D1", "requirement": "string", "weight": "string|null" }
  ],
  "documents_required": [
    { "name": "string", "kind": "insurance|iso|policy|case_study|accounts|other", "notes": "string|null" }
  ],
  "questions_to_answer": [
    { "id": "Q1", "question": "string", "word_limit": "number|null", "weight": "string|null" }
  ],
  "risk_flags": ["string"],
  "estimated_effort_days": "number|null",
  "recommendation": "pursue|decline|review",
  "recommendation_reason": "string"
}
```

The extractor is **not** allowed to invent fields. If the source documents don't say
something, it must mark `confidence: "low"` or omit. Tests must enforce this when
the model output is mocked.

### 5.4 Document vault (Phase 3)

**Storage model**

Every vault document has two layers:

- **Blob**: the actual file in S3 at `s3://{bucket}/vault/{org_id}/{doc_id}/{version}.{ext}`.
- **Record**: ORM row with metadata, version pointer, and structured **claims** —
  a machine-readable summary of what the document proves.

**ORM tables (Phase 3 migration)**

```python
class VaultDocument:
    id: int
    org_id: int
    category: str         # corporate|financial|insurance|accreditation|policy|capability|people|technical|past_bid
    subcategory: str | None
    title: str
    owner_email: str | None
    confidentiality: str  # public|internal|confidential
    current_version_id: int | None
    created_at: datetime

class VaultDocumentVersion:
    id: int
    document_id: int
    version: int          # monotonically increasing
    storage_key: str
    bytes: int
    sha256: str
    mime_type: str
    title: str
    expiry_date: date | None
    issuing_body: str | None
    issued_date: date | None
    last_reviewed_date: date | None
    superseded_by_version_id: int | None
    claims: dict          # JSONB — type-specific structured claims
    text_extracted: str | None
    embedding: vector(1536)  # pgvector for semantic search
    uploaded_by: str
    uploaded_at: datetime
```

**Claims schemas per document type** (these are the machine-readable structures the
re-validation engine queries against):

```jsonc
// insurance_certificate
{
  "doc_type": "insurance_certificate",
  "insurance_type": "employers_liability|public_liability|professional_indemnity|product|cyber",
  "cover_amount": 10000000,
  "currency": "GBP",
  "insurer": "Aviva",
  "insurer_uk_authorised": true,
  "policy_holder": "Acme Ltd",
  "policy_number": "PL-12345",
  "valid_from": "2025-01-15",
  "valid_until": "2026-01-14",
  "territory": "UK"
}

// iso_certificate
{
  "doc_type": "iso_certificate",
  "standard": "ISO 9001",
  "standard_version": "2015",
  "scope": "Provision of cleaning services to public-sector clients",
  "certifying_body": "BSI",
  "certificate_number": "CERT-XYZ",
  "issued_date": "2024-06-01",
  "valid_until": "2027-06-01",
  "holder": "Acme Ltd"
}

// policy
{
  "doc_type": "policy",
  "policy_kind": "health_safety|environmental|equality_diversity|modern_slavery|anti_bribery|data_protection|safeguarding|quality|business_continuity",
  "title": "Health and Safety Policy",
  "covers": ["scope items..."],
  "references_standards": ["ISO 45001"],
  "signed_by_director": true,
  "signatory_name": "Jane Doe",
  "signed_date": "2025-03-01",
  "review_due": "2026-03-01"
}

// case_study
{
  "doc_type": "case_study",
  "client": "NHS Foo Trust",
  "client_sector": "healthcare",
  "client_anonymised": false,
  "value": 750000,
  "currency": "GBP",
  "delivered_from": "2023-04-01",
  "delivered_to": "2024-03-31",
  "services": ["facilities_management", "cleaning"],
  "outcomes": ["12% cost reduction", "98% SLA performance"],
  "team_size": 14,
  "location": "Manchester",
  "consent_to_name_client": true
}

// accounts
{
  "doc_type": "accounts",
  "fiscal_year_end": "2024-03-31",
  "turnover": 8500000,
  "currency": "GBP",
  "profit_before_tax": 620000,
  "audited": true,
  "auditor": "Smith & Co"
}

// dbs_check, professional_qualification, capability_statement, method_statement, etc.
// each have their own claims shape
```

**Re-validation interface**

The vault exposes:

```python
def match_requirement(
    db: Session,
    requirement: dict,        # one of tender_requirements.documents_required entries
    tender_context: dict,     # tender value, dates, sector, etc.
) -> RequirementMatch:
    """Return verdict, candidate documents, reasoning."""

class RequirementMatch:
    verdict: Literal["pass", "partial", "fail", "expired", "ambiguous"]
    documents: list[VaultDocumentVersion]    # ranked candidates
    reasoning: str
    confidence: float
    gaps: list[str]                          # specific shortfalls
```

The matching logic combines:
- **Structured filters** on `claims` (e.g. `insurance_type = "employers_liability"
  AND cover_amount >= 10000000 AND valid_until >= tender.contract_end`).
- **Semantic search** over `text_extracted` and `title` for fuzzier matches
  (especially case studies and policies).
- **LLM judgement** for qualitative fit (e.g. "does this environmental policy meet
  the buyer's requirement for ISO 14001 alignment?") — only when the structured
  filter is ambiguous, and always with the document and the requirement both in
  context. Claude returns a verdict + reasoning that's stored alongside the match.

**Ingestion pipeline**

1. Upload via dashboard, email, or past-bid import.
2. Claude classifies into a category and subcategory.
3. Type-appropriate claims extraction prompt produces the structured `claims` JSON.
4. User reviews and confirms claims (the dashboard surfaces extracted vs raw side by
   side; user can edit before saving).
5. Embedding generated and stored.
6. Version saved; if a previous version exists, it's marked superseded (not deleted).

**Critical invariants**

- Documents are never deleted, only superseded.
- A bid records the exact `VaultDocumentVersion.id` it submitted, not just the
  document_id.
- An expired document is never offered as a match unless explicitly overridden by a
  human, with reason logged.
- No requirement-vs-vault assertion is made without going through the re-validation
  engine. The cached "we have insurance" answer is forbidden.

### 5.5 Drafting agent (Phase 5)

**Input**: a tender with `requirements` and a list of `questions_to_answer`.

**Output**: per question, a draft response that:
- Fits the word limit.
- Cites vault documents by `VaultDocumentVersion.id` for every factual claim.
- Includes a confidence score per claim.
- Is written in the company's tone of voice (learned from approved past bids in
  the vault).

The drafting agent is implemented as a tool-using Claude session:

- `tool: vault_search(query, filters)` — semantic + structured search over the vault.
- `tool: vault_get(document_id, version_id)` — fetch full claims and extracted text.
- `tool: past_response_search(question_pattern)` — find similar past responses.
- `tool: flag_for_review(reason, draft_so_far)` — surface uncertainty.

**Hard rules**

- No claim without a cited vault source.
- If a question requires a fact the vault doesn't support, the agent **flags for
  review and drafts a placeholder**, never invents.
- Quoting from past responses verbatim is fine (it's our own content); quoting from
  the buyer's ITT requires paraphrase unless the ITT is being reused as boilerplate.

### 5.6 Case study generator (Phase 5)

**Input**: project folder containing notes, emails, photos, completion certificates,
client communications.

**Processing**:
1. Vision-enabled Claude reads photos, OCRs documents, summarises notes.
2. Structured extraction: client, sector, dates, value, services, challenges,
   solutions, outcomes, team, location.
3. Targeted interview to fill gaps (3-8 questions max).
4. Photo curation — quality filter (Claude vision scoring), aspect-ratio crops
   (subject-aware), brand-consistent colour grading, privacy redaction prompts.
5. Narrative draft: challenge → approach → solution → outcome → metrics.
6. Layout via **Typst** templates (one-pager, two-pager, four-pager). Output: PDF +
   editable .docx.
7. Vault ingestion with `case_study` claims schema.

Templates live in `tender-agent/assets/case_study_templates/` and must be built by a
designer before this phase ships. The agent populates `data.toml` and Typst handles
layout.

**Per-tender adaptation**: when attached to a bid, the case study is re-versioned to
foreground the criteria that bid is being evaluated on (social value, technical
capability, etc.). Both versions are stored, linked.

### 5.7 Portal adapters (Phase 4)

**Interface every portal adapter implements**:

```python
class PortalAdapter(Protocol):
    name: str
    base_url: str

    async def login(self, page: Page, creds: PortalCredentials) -> None: ...

    async def register(
        self,
        page: Page,
        registration_data: dict,
        approval_callback: ApprovalCallback,
    ) -> RegistrationResult:
        """Fill the registration form. Call approval_callback before submitting.
        Returns credentials to store in Secrets Manager."""

    async def find_tender(self, page: Page, portal_ref: str) -> PortalTender: ...

    async def download_documents(
        self,
        page: Page,
        tender_ref: str,
    ) -> list[DownloadedDocument]:
        """Returns documents staged on disk plus metadata."""

    async def check_amendments(
        self,
        page: Page,
        tender_ref: str,
        since: datetime,
    ) -> list[Amendment]: ...

    async def stage_submission(
        self,
        page: Page,
        tender_ref: str,
        bid_package: BidPackage,
    ) -> StagedSubmission:
        """Fill all forms, upload all documents, go to the final review screen.
        Do NOT click Submit. Return a screenshot + summary for human review."""

    async def confirm_submission(
        self,
        page: Page,
        staged: StagedSubmission,
    ) -> SubmissionReceipt:
        """Only called after explicit human approval via dashboard."""
```

**Portals in scope** (order of priority — pick one for Phase 4):

| Portal | Vendor | Notes |
|---|---|---|
| ProContract | Proactis | Common across local authorities |
| In-Tend | In-Tend Ltd | NHS, councils |
| Jaggaer/Bravo | Jaggaer | CCS, large authorities |
| Delta eSourcing | Delta | Education, NHS |
| Atamis | Atamis | NHS England |
| Multiquote | Multiquote | Smaller authorities |

**Adapter rules**

- One file per portal under `services/portals/`. Subclass a `PortalAdapterBase` that
  handles browser context, retries, screenshot capture on error.
- Selectors must be in module-level constants at the top of the file so they're easy
  to update when the portal redesigns.
- Every login uses credentials fetched from Secrets Manager; **never read from DB,
  never log credential values**.
- Every page load captures a screenshot to S3 (debug bucket) tagged with bid_id +
  timestamp. Essential for debugging and audit.
- Every adapter has integration tests that run against a recorded fixture (using
  Playwright's HAR replay). Live tests run against staging only, never prod.
- Captcha handling: if a captcha appears, stop and require human resolution via the
  dashboard. Do not attempt to solve.

### 5.8 Email and notification layer (Phase 6)

Two providers, same interface:

- **Gmail** via Gmail API + OAuth.
- **Microsoft 365** via Microsoft Graph + OAuth.

A `MailboxProvider` Protocol abstracts both. Inbound: watch a designated address
(e.g. `tenders@yourdomain`), parse incoming mail, classify as
clarification/amendment/award/other, attach to the right tender via subject-line
references or sender domain matching, surface to the dashboard.

Outbound: never autonomous. Replies to buyers are drafted by the agent and queued
for human approval, then sent via the user's OAuth-connected account so threading
and identity remain correct.

### 5.9 Workflow engine (Phase 6)

Temporal workflows model long-running bids:

```python
@workflow.defn
class BidWorkflow:
    @workflow.run
    async def run(self, tender_id: int) -> BidResult:
        brief = await workflow.execute_activity(generate_brief, tender_id, ...)
        go = await workflow.wait_for_signal("go_no_go_decision", timeout=14*days)
        if not go:
            return BidResult(status="declined")

        compliance = await workflow.execute_activity(build_compliance_matrix, tender_id, ...)
        await workflow.wait_for_signal("compliance_approved")

        draft = await workflow.execute_activity(draft_responses, tender_id, ...)
        await workflow.wait_for_signal("draft_approved")

        package = await workflow.execute_activity(build_bid_package, tender_id, ...)
        await workflow.wait_for_signal("submission_approved")  # MANDATORY HUMAN

        receipt = await workflow.execute_activity(submit_bid, package, ...)
        return BidResult(status="submitted", receipt=receipt)
```

Deadline reminders are scheduled child workflows. Activities that interact with
Claude or portals are idempotent and retried with exponential backoff.

---

## 6. Data model summary

Already in `models.py`: Source, Tender, FilterProfile, FilterMatch, PollRun,
TenderDocumentFile, TenderRequirements.

To add in later phases:
- VaultDocument, VaultDocumentVersion (Phase 3)
- Brand, BrandAsset (Phase 3 — brand kit for case study generator)
- PortalCredentialRef (Phase 4 — references Secrets Manager; no plaintext in DB)
- Bid, BidResponse, BidAttachment, BidAuditLog, Submission (Phase 4)
- MailboxAccount, MailboxMessage (Phase 6)

---

## 7. Human checkpoint policy

This is **non-negotiable**. Phase deliverables fail acceptance if they violate this.

### Always human-gated (mandatory click)

1. **Final submission of any bid** — agent stages, human submits.
2. **Portal registration** — agent fills the form, human reviews and submits.
3. **Outbound communication to a buyer** — clarifications, RFI responses, withdrawals.
4. **Vault document approval** — claims records are confirmed by a human before the
   document is considered authoritative for matching.
5. **Pricing decisions** — bid pricing is never set by the agent.

### Configurable per filter profile or per tender

6. **Go/no-go after the brief**.
7. **Compliance matrix sign-off** (especially gap acceptances).
8. **Draft response approval** per section.
9. **Document selection** when the vault has multiple candidates.

### Auto-proceed by default

10. **Tender discovery and matching**.
11. **Document download**.
12. **Brief generation**.
13. **Compliance matrix generation**.
14. **Amendment monitoring**.
15. **Internal alerts to the bid team**.

### Implementation

Checkpoints are realised as Temporal `wait_for_signal` blocks. The dashboard has a
"Review queue" page listing all bids awaiting a signal. Each item has the diff, the
context, and approve/reject actions. Every approval records: who approved, when,
what the approved-of payload hash was. This is the audit trail.

---

## 8. Legal and operational guardrails

- **Portal terms of service** vary. Before enabling a portal adapter for a real
  buyer, the company must read and accept that portal's ToS. Some forbid automation;
  for those, the adapter is disabled and the workflow falls back to manual steps.
- **GDPR / UK data protection**: tender documents often contain personal or
  confidential information. Everything is stored in UK or EU regions. Encryption at
  rest is mandatory (RDS encrypted, S3 SSE-S3 minimum, KMS preferred).
- **Audit log**: every agent action that affects an external system or a vault
  record is logged with timestamp, actor (agent or user), input hash, output hash,
  and outcome. Retention 7 years to align with public-sector procurement standards.
- **Bid submissions are legally binding offers**. The submission checkpoint exists
  precisely because a misfiled bid can expose the company to contract liability.
- **Copyright in drafting**: the agent never copies large blocks of the buyer's ITT
  verbatim into the response. Quotes from the ITT are short and clearly framed.
  Past-response reuse is fine because it's the company's own content.
- **Disclosure**: if a buyer asks whether AI was used to prepare the bid, the company
  must be able to answer truthfully. The agent logs all model-generated content so
  this question can be answered factually.

---

## 9. Deployment (AWS)

**Phase 2 target architecture**

- **Compute**: ECS Fargate cluster, two services: `api` (FastAPI) and `worker`
  (scheduler + background tasks).
- **Database**: RDS PostgreSQL 16, single-AZ for staging, multi-AZ + read replica
  for prod. `pgvector` extension enabled.
- **Object storage**: Two S3 buckets — `tender-agent-documents-{env}` (tender attachments
  and vault), `tender-agent-debug-{env}` (portal screenshots, audit artefacts).
- **Secrets**: AWS Secrets Manager for Anthropic API key, portal credentials, OAuth
  tokens, VAPID keys.
- **Networking**: ALB in public subnets → ECS in private subnets. RDS in private
  subnets only. NAT gateway for outbound.
- **Frontend**: Dashboard built as a Next.js standalone, deployed to ECS or
  alternatively Vercel (Vercel is simpler for the dashboard; Fargate keeps everything
  in one VPC).
- **CDN**: CloudFront in front of the dashboard for SSL + caching of static assets
  and the service worker.
- **Observability**: CloudWatch Logs from Fargate, Sentry for errors, optional
  Grafana Cloud for metrics.

**Phase 6 additions**

- Temporal Cloud (recommended over self-hosting) for workflows.
- Playwright workers run on Fargate with a larger task definition (browser is
  heavy); dedicated `playwright` ECS service.

**IaC**: Terraform under `terraform/`. Modules: `vpc`, `rds`, `ecs_service`, `s3`,
`secrets`, `cloudfront`. Environments under `terraform/envs/{staging,prod}/`. CI
runs `terraform plan` on PR, `terraform apply` on merge to `main` after manual gate.

---

## 10. Conventions

**Code**

- Python: Ruff for lint, 100-char lines, type hints everywhere, `from __future__ import annotations` at the top of every module.
- TypeScript: strict mode on, no `any` (use `unknown` and narrow).
- No business logic in API route handlers — they call into `services/`.
- Every service function takes `db: Session` (or `db: AsyncSession`) as its first
  argument when it touches the database. No hidden globals.

**Errors**

- Adapters and external calls use Tenacity with exponential backoff + jitter, capped
  at 4 attempts.
- The ingestion pipeline commits per record so a later failure doesn't lose progress.
- `try/except Exception` is acceptable at outer boundaries (a poll run, a bid
  workflow activity) but never inside helpers.

**Testing**

- Every new service function ships with a unit test.
- Integration tests for adapters use HAR replay; never hit live portals from CI.
- Test data uses realistic OCDS shapes from `tests/fixtures/`.

**Migrations**

- Every schema change is a new Alembic revision. Never edit a merged migration.
- Migrations are reversible: implement `downgrade()`.

**Logging**

- `structlog` everywhere. JSON output in production, console-friendly in dev.
- No PII in logs. Log document IDs and hashes, not contents.
- No credentials, ever.

**Secrets**

- All secrets via environment variables locally and Secrets Manager in AWS.
- Never commit a `.env` file. `.env.example` documents required keys.

---

## 11. Glossary

- **CPV** — Common Procurement Vocabulary. EU classification system the UK still
  uses for categorising procurement.
- **OCDS** — Open Contracting Data Standard. JSON schema many UK and global tender
  portals publish in.
- **OCID** — Open Contracting ID. The unique identifier for a procurement in OCDS.
- **ITT** — Invitation to Tender. The primary tender specification document.
- **PQQ / SQ** — Pre-Qualification Questionnaire / Selection Questionnaire. The
  first-pass eligibility filter on a tender.
- **DPS** — Dynamic Purchasing System. A multi-vendor framework that admits new
  vendors at any time.
- **Framework** — A pre-qualified vendor list a buyer can run mini-competitions
  against without a full tender.
- **Award notice** — A post-decision notice naming the winner.
- **CCS** — Crown Commercial Service. Central UK government's procurement arm.
- **Claim** — In the vault, a structured fact a document proves. Re-validation
  queries against claims, not against the file content.
- **Compliance matrix** — Per-requirement verdict (pass/fail/partial/expired/missing)
  for a tender's documents-required list.
- **Bid package** — The complete set of responses + attachments staged for
  submission.
- **Staged submission** — Everything filled in on the portal up to the final
  Submit screen, awaiting human approval.

---

## 12. What's actually done as of this commit

- ✅ Phase 1 complete (Pass 1 zip).
- ⏳ Phase 2 ~70%: backend done, dashboard scaffold built but pages incomplete,
  AWS infra not yet written. See `tender-agent/` and `tender-agent-dashboard/`.
- ⬜ Phases 3-7 designed but not implemented.

If you're a developer or Claude Code picking this up: finish Phase 2 first (dashboard
pages + Terraform), then run Phase 3 acceptance against the spec in §5.4.
