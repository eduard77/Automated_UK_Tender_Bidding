# Phase 4 — Portal Infrastructure Layer

## Status
Design locked. Build in progress via sequenced autonomous Claude Code prompts.
See `docs/phase-4-prompts/` for build sequence.

## Purpose
When the user clicks "Generate brief" on any UK public-sector tender, the system
handles whatever portal it lives on automatically — discovering, logging in or
registering, registering interest, downloading documents, producing a 
Moredon-style two-page brief.

The product is multi-sector procurement intelligence. Construction is the user's
current focus but the system must remain sector-neutral throughout.

## Core principle: machines act, humans interpret

Mechanical work runs autonomously: polling APIs, downloading files, parsing
documents, generating briefs, updating the database.

Every moment of interpretation pauses for the user:
- Before any email is sent to a buyer (draft for approval)
- When any email reply arrives (user reads before system proceeds)
- After portal documents finish downloading (user confirms completeness)
- After brief generation completes (user reads brief before pipeline entry)
- When a portal adapter encounters an unexpected state
- When the classifier flags a new portal (user confirms classification)
- When a tender shows requirements the vault can't satisfy
- Before any portal "register interest" click

The system never grabs the user's attention without permission. Notifications
are always opt-in pulls, never auto-opens.

## The eight components

### 1. Portal discovery
Scrapes every UK tender source (Contracts Finder, Find a Tender, Public 
Contracts Scotland, etc.) for external URLs. Extracts URLs from tender 
descriptions, document arrays, contact fields, additionalInformation. Normalises
to domain. Filters out non-portal noise via database-driven blocklist 
(manageable from dashboard). Clusters into portal records.

When a new portal appears, Claude classifies it (level B from design):
fetches portal homepage, sends domain + sample tender URL + truncated HTML to
Claude, gets back is_procurement_portal / confidence / suggested_display_name /
login_type / suggested_priority / notes. Result stored in classification_data
JSONB on portal record.

### 2. Portal registry
Two tables:
- `portals` — one row per unique portal domain. Fields: id, domain (unique, 
  lowercase, no scheme), display_name, url_patterns (jsonb), login_type 
  (none/email_only/username_password/2fa/oauth/unknown), adapter_status 
  (not_started/stub/read_only/full/deprecated), adapter_module, priority 
  (critical/high/medium/low), tender_count, first_seen_at, last_seen_at, 
  classification_data (jsonb), notes, created_at, updated_at.
  Indexes: domain (unique), adapter_status, priority, last_seen_at desc.
- `portal_url_sightings` — one row per URL extracted. Fields: id, portal_id 
  FK nullable, tender_id FK, url, extracted_from 
  (description/additional_information/documents/contact/parties), sighting_type
  (tender_link/document_link/contact_email/reference_text), extracted_at.
  Indexes: portal_id, tender_id, composite (portal_id, extracted_at desc).

Plus `portal_blocklist_domains` — DB-driven blocklist of domains we know aren't
portals (gov.uk policy pages, social media, source domains, etc.). Managed via
dashboard.

The `/portals` dashboard page is full CRUD: view, edit, override classification,
manually set priority, mark adapter status, etc.

### 3. Universal adapter framework
Every portal adapter implements a common interface (`PortalAdapter`) with six
methods:
1. `matches_url(url)` — classifier
2. `authenticate(ctx, creds)` — log in, return AuthResult 
3. `locate_tender(ctx, tender_ref)` — navigate to specific tender
4. `register_interest(ctx)` — click "register interest" / equivalent
5. `download_documents(ctx, dest_dir)` — pull all ITT documents
6. `screenshot(ctx, label)` — default impl, for audit

Each method returns a structured result with status enum, never raw data. 
Status enums cover known outcomes (success / needs_registration / 
requires_email_confirmation / access_denied / etc). Orchestrator branches on 
each.

Browser context: persistent per portal per user, stored at 
`~/.tender-agent/browsers/{user_id}/{portal_id}/`. Cookies and local storage 
persist. Headless by default, headed for debugging. Anti-bot baseline 
(playwright-stealth, realistic delays, real user-agent, no obvious automation 
tells).

Credentials: encrypted SQLite database, key in OS keyring (Windows Credential 
Manager / macOS Keychain / Linux Secret Service). Per-portal record: portal_id,
user_id, username, password (encrypted), email, extra (jsonb), last_used_at, 
last_validated_at, valid (bool). Migrates to AWS Secrets Manager on deployment;
interface unchanged.

Fallback adapter (`FallbackAdapter`): for portals in registry without dedicated
adapter. Tries to download publicly accessible documents without auth. Returns
partial DownloadResult flagging "everything behind login" as missing.

### 4. Registration walkthrough — lazy

Registration on a portal only happens when the user clicks "Generate brief" on 
a tender that requires it. Never proactive. Never bulk.

Flow: system detects need to register → opens browser window in front of user →
navigates to portal's registration form → fills in everything from stored 
company profile (name, registration number, VAT, address, contact) → user 
completes portal-specific fields and submits → system watches for confirmation 
email → clicks confirmation link → credentials stored → brief generation 
continues.

Time: ~3-5 minutes per portal, one-time. Subsequent tenders on that portal use 
stored credentials.

### 5. Email path

For tenders that are email-only (no portal URL, just a contact like 
"ops@buyer.gov.uk"):

1. User clicks "Generate brief"
2. System detects no portal URL, drafts request email in user's voice
3. User reviews draft, can edit, clicks Send
4. System sends from user's connected inbox (eduard.szigeti@genera-systems.com 
   for now)
5. System monitors inbox for reply
6. When reply arrives: ALWAYS flag for user attention (strict principle). User 
   reads, decides whether to use the attached documents or respond manually.
7. If user approves: documents fed to brief engine

If no reply in 3 working days: system drafts polite follow-up, user approves, 
sent.

Inbox: connected via Gmail OAuth / Microsoft Graph OAuth (Phase 6 work).

### 6. Brief generation engine

Inputs: tender metadata + all downloaded documents.

Process:
1. Convert all documents to plain text (pdfplumber / python-docx / etc.)
2. Send to Claude (Sonnet) in chunks — Claude reads everything thoroughly (B 
   strategy: thorough chunking, not summarisation)
3. Receive structured brief data — every section a named JSON field, not free 
   text
4. Render to on-screen HTML and to PDF

Brief is ALWAYS two pages. No exceptions. When Claude judges a topic needs more 
depth than fits, it generates a `<deep_dive_link>` placeholder in the structured
output. The UI renders these as clickable links. Clicking generates the 
deep-dive page on demand (lazy, not pre-generated).

Recommendation field is ALWAYS advisory. Output schema requires 
"recommendation_summary" (Bid / Bid with caveats / No bid) plus 
"recommendation_reasoning" plus "things_to_verify" (list). The brief renders 
these as advice with explicit caveats; UI never shows a verdict-style green/red
verdict.

Cost: ~£0.50-£1.50 per brief (Sonnet, chunked, large documents). Acceptable 
for development. Cost monitoring is a Phase 7 concern.

### 7. Brief view

Two views of the same content:
- **On-screen**: `/tenders/[id]/brief` in Elevated Genera theme (dark canvas, 
  mint accent, Fraunces serif, Inter body). Scrollable but visually paginated 
  into two sections. Six actions at top: Download PDF, Pursue this tender, 
  Pass, Regenerate brief, View source documents, Notes.
- **PDF**: A4 portrait, two pages. White background, print-friendly. Header on 
  each page (Genera Tenders, date generated, tender source ref). Footer on each
  page: "Generated by Genera Tenders. Recommendation is advisory. Verify all 
  details against source documents before bid submission."

Every fact in the brief has a "where did this come from?" tooltip showing the 
source document and page.

Deep-dive links open dedicated pages at `/tenders/[id]/brief/{deep-dive-slug}`.
Each deep-dive is its own page, own URL, optionally its own PDF.

### 8. Vault matching

The brief's page 2 includes a Pass/Fail compliance checklist. Every ITT 
requirement gets a row:
- The requirement text
- Status: Met / Met but check / Gap / Not a vault thing
- Evidence: which vault doc, or "not in vault"
- Action: Upload (for gaps), View evidence (for matches)

Status logic:
- **Met**: vault contains clear match
- **Met but check**: vault has right category but Claude's not 100% sure 
  (amber row, e.g. "Your PI is £5m, requires £10m. Confirm cover or upload 
  updated cert.")
- **Gap**: nothing in vault. Red row with Upload button. Upload uses existing 
  vault upload modal pre-categorised.
- **Not a vault thing**: requirement is about company behaviour, not a 
  document. Shown as judgment item, no action.

Manual ITT upload: every tender has "Upload ITT manually" as a clear secondary 
action under "Generate brief". When user uploads manually, automated fetch is 
cancelled. Documents stored attached to tender record. Brief generation runs 
against manual files.

### 9. Continuous monitoring

Daily background health checks for: adapter health (probe each portal 
homepage), credential health (lightweight login test), document fetch 
reliability (rolling 7-day failure rate), brief generation reliability, new 
portals.

Dashboard: `/system-health` page showing adapter status (green/amber/red), 
credentials status, recent failures, new portals awaiting classification, 
attention items.

Push notifications: TWO separate channels (user can mute one without the 
other):
- **Tender alerts** — new matched tenders
- **System alerts** — credential expired, adapter broken, high-priority new 
  portal needs review

What gets pushed (system alerts):
- Credential broke
- Adapter fully broken (not just degraded)
- High-priority new portal needs classification confirmation

What stays in the dashboard (no push):
- Daily check results
- Single failures
- Low-priority new portals
- Stats updates

Auto-recovery: light only. System re-tries simple things mechanically 
(re-login if session expired, retry transient errors) but escalates to user 
for anything interpretive. Consistent with core principle.

### 10. User journey checkpoints

Standard flow from "click Generate brief" to "brief in pipeline":

1. **Checkpoint 0** (only if needed): registration walkthrough OR email draft 
   approval. Skipped if user already has portal credentials and tender is on a 
   portal.

2. **System fetches documents** — autonomous, ~30-90 seconds. UI shows progress.

3. **Checkpoint 1**: User confirms documents received. Sees file list. Clicks 
   Continue.

4. **System generates brief** — autonomous, ~60-120 seconds.

5. **Push notification: "Brief ready"**. User opens it when convenient. NEVER 
   auto-opens.

6. **Checkpoint 2**: User reads brief. Decides Pursue or Pass.

7. **If Pursue**: tender enters bid pipeline at Researching stage.
   **If Pass**: tender archived with reason (9 standard reasons).

Total user time: 3-7 minutes per tender. Pipeline value triage.
