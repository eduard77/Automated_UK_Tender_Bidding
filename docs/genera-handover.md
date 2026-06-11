# Genera Tenders — Handover & State of Play
*Written 11 June 2026, after a two-day build push. Read this fresh, then decide the build order — don't build from tired memory.*

---

## 1. The one-paragraph summary

Proactis logged-in discovery now works end to end, including the hard parts (login, cookie wall, and — finally — CPV category filtering). The dashboard is live in a browser for the first time. Hundreds of real tenders are flowing in. Along the way, a clear **product vision** emerged (per-user dashboards driven by a guided setup page, with daily notifications) — and with it, the realisation that the **central architectural job is cross-source field normalisation**, plus that going multi-user is a bigger lift than it first looks. Several sources still aren't pulling. Nothing below needs doing tonight.

---

## 2. What is working now (proven this session)

- **Proactis discovery, fully:** login + cookie dismissal + **CPV category filtering** (all five construction CPVs — 45, 71, 50, 44, 09 — tick correctly; PR #117 fixed the last detection bug). Confirmed live: `applied: 5`.
- **Real data flowing:** an unfiltered Proactis run pulled 200+ tenders across all portals (20 pages × 10); a CPV-filtered run inserted 61 construction tenders before erroring at page 17.
- **Dashboard live in browser:** `https://genera-tenders-dashboard-bgg7aqewf8f0c0ge.ukwest-01.azurewebsites.net`
  - `/search` page works: Region, Value, Status, Source filters; click into tender detail.
  - Reads live from the backend (CORS + cross-site session fixed, PR #114).
- **Filter profiles (the engine for per-user views) exist** and work: create via `POST /filters`, drive discovery via `run-for-profile`. Profile 1 = construction (CPV 45,71,50,44,09).
- **Sources currently in the dashboard:** CF, FTS, PCS, Proactis/ProContract, SAMPLE_SEED (test data).
- **Region filtering works for Proactis** (e.g. "Greater Manchester" shows on tenders).
- Earlier-proven and still live: auto-deploy on merge, email-catching feature, encrypted credential store, per-user push notifications, EU-Supply & Atamis adapters (built/deployed — but see "not pulling" below).

---

## 3. The key finding of the day (why dashboard CPV filtering "shows nothing" for Proactis)

**Proactis tenders arrive with no CPV code stored on them.** Proactis lets you *search* by CPV (which we got working), but it does **not show a CPV on each result**, so nothing CPV-shaped gets saved per tender. Confirmed by inspecting a real tender's detail (region, value, keywords present; no CPV).

Consequence: the dashboard's CPV filter correctly finds nothing for Proactis, because the field is empty — even though the construction tenders are in the database. The filter isn't broken; the data field was never populated.

**This is an instance of the bigger issue (section 5): each source exposes different fields.** CF/FTS publish CPV; Proactis doesn't; Atamis/EU-Supply unknown until they pull.

---

## 4. The product vision that emerged (capture — not yet designed properly)

A per-user, guided-setup, multi-source feed:

1. User types a plain word (e.g. **"construction"**) in a **setup page**.
2. System shows **all CPV codes related to that word**; user **picks** the ones they want.
3. User also sets **region**, **value range**, **keywords (include & exclude)**, and can **include/exclude specific sources**.
4. Saved → becomes **that user's dashboard** (they can create several; a different category set = a new dashboard).
5. Database populates for those choices, **across all sources uniformly**.
6. User gets a **daily notification** of new matching tenders.

**What already exists for this:** filter profiles (engine), discovery/population, source field per tender, push notifications, live dashboard with region/value/source/status filters.

**What's new to build:** the guided **setup page** (word → CPV picker → save); **source include/exclude** on the profile; the **daily digest** wiring; sensible **CPV presets** (most users won't know their codes — offer "Construction & trades" bundles).

---

## 5. The hard foundation: cross-source normalisation (the real spine)

"It must work for all sites, not just Proactis" is the central architectural requirement. For one filter to work uniformly across CF, FTS, PCS, Proactis, EU-Supply, Atamis, every tender must land in the database with the **same normalised fields** (CPV, region, value, dates), regardless of origin.

Reality per source:
- **CF / FTS:** rich — publish CPV directly. Filter fine already.
- **PCS:** structured, similar.
- **Proactis:** thin — **no CPV on listings.** Needs enrichment (see options below).
- **Atamis / EU-Supply:** unknown — **not pulling yet**, so can't even assess their fields.

**Options to give Proactis tenders a CPV (a design choice, pick later):**
- **(a) Stamp the searched CPV at save-time** — when a run filtered by 45/71/50/44/09 returns a tender, tag it with those codes. Cheap; approximate (knows it matched one of the active set, not which). Probably the right trade.
- **(b) Open each tender's detail page and read whatever fields it carries** — more thorough, much slower.
- **(c) Accept Proactis filters by region/keyword/value only**, lean on CF/FTS for CPV.

Getting normalisation wrong is expensive to unwind — this deserves a clear-headed, source-by-source plan, not a tired prompt.

---

## 6. Design backlog / things easy to miss (from the brainstorm)

**Filtering model:**
- Keywords (include *and* exclude) are often more useful than CPV for SMEs, and catch mis-/un-coded tenders. Profiles already have a keywords field — make it first-class.
- Value thresholds per user (small builder vs large). Exists; expose in setup.
- Buyer include/exclude (follow specific councils, or never see certain buyers).
- CPV picker UX: users don't know their codes — offer presets/bundles, not a raw tree.

**Duplicates:** the same tender appears on CF *and* a portal. Dedup exists, but per-user views and digests must respect it or the product looks broken (same opportunity twice = looks broken).

**Per-tender state (essential, currently missing):** users need to mark interested / not-interested / applied, so digests stop re-showing rejected items. Without this the feed becomes noise.

**Deadlines / reminders:** arguably more valuable than discovery alerts — "expression of interest closes in 3 days." Not built.

**Daily notification decisions:** send time / timezone; email vs push vs both; first-setup backlog handling (don't dump everything); "nothing new today" — send or stay silent.

**The big one — multi-tenant is a major lift:** real user accounts, login, billing, and strict per-user data isolation. Today there's effectively one operator (you). "Per-user dashboards" hides this whole layer. See it clearly before committing.

**Legal/ops:** ToS, data protection (storing buyer contacts and, soon, customer data), each portal's terms on automated access.

---

## 7. Open technical items (carried over, not yet resolved)

1. **Atamis, EU-Supply, Sell2Wales not pulling.** None appears in the dashboard Source facet. Built/deployed but no data. **Diagnosis never completed** — open the backend **Log stream** and look for `atamis`, `eu_supply`, `sell2wales` lines:
   - `*.fetch_failed` → datacenter 403 block (like Delta/Proactis-hosts) → fix = route through the disguised bridge.
   - `*.pagination_stalled` → pagination fix.
   - no lines at all → scheduler not running them (registration issue).
2. **Run 15 errored at page 17** (`profile_failed` after inserting 61 rows). Worth a look — it pulled real data but didn't finish cleanly.
3. **Proactis portal-name corrections** (if you ever use per-portal scoping again). Real dropdown names from the diagnostic:
   - Use: `The Chest`, `London Tenders`, `Supplying The South West`, `South East Business Portal`, `EastMidsTenders` (one word), `Supply Stoke and Staffordshire`.
   - **YPO and ESPO are NOT in the Proactis dropdown** — they're not portals on this platform; drop them.
   - Northern coverage: The Chest (NW); individual buyers exist (Tees Valley/Teesworks, Advance Northumberland, South Yorkshire MCA, Leeds Teaching Hospital, etc.); **YORtender = EU-Supply** (blocked on item 1); **NEPO = separate platform, needs manual registration.**
4. **Junk profile 2** exists (created with `"string"` placeholder values). Either fix via `PATCH /filters/2` with empty lists, or ignore/delete it.
5. **Email feature one-time OAuth registration** still pending (Google/Microsoft app setup per `docs/email-setup.md`; secrets onto Azure).

---

## 8. Recommended build order (when fresh — confirm it still feels right first)

1. **Decide the Proactis CPV approach** (5a stamp-at-save is the likely pick) — unblocks construction profiles catching Proactis tenders.
2. **Finish the source diagnosis** (item 7.1) — get Atamis/EU-Supply/Sell2Wales actually pulling, so "all sources" is true.
3. **Design the normalisation layer** properly, source by source (section 5) — the spine.
4. **Then** build the user-facing vision (section 4): setup page → CPV picker/presets → source include-exclude → per-user dashboards → per-tender state → daily digest.
5. **Separately scope the multi-tenant lift** (accounts/billing/isolation) — it's a project of its own.

---

## 9. Key facts / addresses

- Repo: `github.com/eduard77/Automated_UK_Tender_Bidding`
- Backend API/docs: `https://generatender-gqbgaye9fmdfc4c6.ukwest-01.azurewebsites.net/docs`
- Dashboard: `https://genera-tenders-dashboard-bgg7aqewf8f0c0ge.ukwest-01.azurewebsites.net` (`/search`, `/portals`)
- Proactis portal_id = 348; construction FilterProfile id = 1
- Deploys: merge → GitHub Actions auto-deploys; wait for green + ~60–90s startup (migrations run on boot)
- A run fired seconds before an env-var-change restart gets orphaned — re-fire after the restart settles
- PRs this session: #114 dashboard live + CORS/cookie; #116 + #117 Proactis CPV popup + portal dropdown + detection fix (merged)
- **Housekeeping:** Docker Desktop + a db container are still running on your laptop from last night's tests — quit them when convenient.

---

*Bottom line: the hard engineering (Proactis end-to-end, live dashboard, real data) is done. What's left is mostly product design and a normalisation plan — both better done rested than at the end of a marathon. Sleep, re-read, then sequence it.*
