TASK: Anonymous search + free-alert accounts + tiered brief access + submission-package %-fee + Stripe (TEST MODE, env-gated)

GOAL (plain English)
Search stays free and anonymous (no login). Three revenue lines:
1) BRIEFS — viewing a tender's full brief AND its full source documents requires brief access. Access via either pay-as-you-go (£10, one brief+its docs) OR a monthly plan (100 generations/month, or unlimited). Non-payers see HALF of the brief and HALF of each document as a teaser.
2) SUBMISSION PACKAGE — generating a bid submission package costs 0.5% of the tender's contract value, clamped to a £100 minimum and £300 maximum, charged UPFRONT as a one-off when they generate it. Never on subscription; the brief plans never include it.
3) FREE ALERTS — a free account (no payment) to receive alerts.
Build the real Stripe flow in TEST MODE; it must be inert (app fully usable) until Stripe keys exist in .env, and every gate must be testable WITHOUT Stripe via a dev-only flag.

STRIPE PRICE IDS (test mode — store as env/config constants, do NOT hardcode amounts in logic):
- STRIPE_PRICE_BRIEF_PAYG   = price_1TdURiDFsiLtuBZSzRPop0Rr   (£10 one-off, one brief + its docs)
- STRIPE_PRICE_PLAN_100     = price_1TdUSTDFsiLtuBZSKmhzgXfy   (£100/mo, up to 100 brief generations)
- STRIPE_PRICE_PLAN_UNLIM   = price_1TdUSmDFsiLtuBZSihjbMA8k   (£250/mo, unlimited generations)
Submission package fee has NO Stripe price — computed at runtime (see PART D).

PART A — SEARCH STAYS PUBLIC
- /search and all discovery stay fully anonymous. Do NOT add auth to search. Anonymous = free/locked.

PART B — ACCOUNTS
- New Alembic migration: accounts table — id, email (unique, not null), password_hash, created_at,
  plan enum('free','payg','plan_100','plan_unlimited') default 'free',
  stripe_customer_id (nullable), current_period_end (nullable timestamptz),
  brief_generations_this_period (int default 0), period_anchor (timestamptz nullable).
- New table brief_entitlements: id, account_id FK, tender_id FK, granted_at, source enum('payg','plan','dev') — one row = that account may see the FULL brief + FULL docs for that tender. (PAYG grants one tender; plans grant on generation.)
- Email+password signup + login. Hash with bcrypt/argon2 — NEVER store/log plaintext. Reuse the app's existing auth pattern if one exists; else httpOnly secure cookie sessions.
- GET /me returns plan, period usage/limit, and is for driving the UI. Anonymous → free.
- DEV-ONLY override (REFUSE if TENDER_AGENT_ENV=production): set an account's plan, and grant a brief_entitlement for a tender, so all gates are testable before Stripe keys exist.

PART C — BRIEF + DOCUMENT GATE (server-enforced, 50% teaser)
- Enforce ENTIRELY server-side; the API must NOT return locked content to the unentitled. Frontend only renders the blur/CTA — never rely on it to hide data.
- ENTITLED for a tender = (active plan_100/plan_unlimited within its monthly allowance) OR (a brief_entitlements row for that tender, e.g. from PAYG). Entitled → FULL brief JSON + FULL document extracted text + downloads enabled.
- NOT entitled → PREVIEW:
  * BRIEF: return an allow-listed half — headline + scope_summary + counts (e.g. "3 key risks identified"); REDACT recommendation text, rationale, key_risks detail, figures, deadline specifics. locked:true + "unlock" marker.
  * DOCUMENTS: first 50% of each document's extracted text (named constant PREVIEW_FRACTION=0.5); remainder replaced by a lock marker; downloads return 402/locked, not bytes.
- PLAN METERING: plan_100 = up to 100 brief generations per monthly period (track brief_generations_this_period vs limit, reset at period_anchor/current_period_end). plan_unlimited = no cap. Generating a brief on a NEW tender consumes one generation and creates a brief_entitlement(source='plan'). Re-viewing an already-entitled tender does NOT consume another. When a plan_100 user hits 100, generation returns a clear "monthly limit reached — upgrade to Unlimited or buy a single brief (£10)" state.

PART D — SUBMISSION PACKAGE %-FEE (one-off, computed at runtime)
- Fee = round(0.5% of the tender's contract_value), clamped to MIN £100 and MAX £300 (named constants SUBMISSION_FEE_PCT=0.005, SUBMISSION_FEE_MIN_GBP=100, SUBMISSION_FEE_MAX_GBP=300). If contract_value is unknown/null → default to the £100 minimum.
- Charged UPFRONT, one-off, when the user requests package generation. Implement via a Stripe Checkout Session in PAYMENT mode with a dynamically-created line item for the computed amount in GBP (price_data on the fly — NO pre-made Stripe price). On payment success (webhook), unlock/queue the package generation for that tender for that account.
- Never on subscription. Brief plans grant NO package access.

PART E — STRIPE (TEST MODE, env-gated, inert without keys)
- Env: STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET, plus the three price-ID envs above. If any required key is absent → payment endpoints return a clear "payments not configured" state and the WHOLE app still works (gates run off plan/entitlement + dev flag).
- POST /billing/checkout (auth required): body picks one of {payg_brief(tender_id), plan_100, plan_unlimited, submission_package(tender_id)}. Creates the appropriate Checkout Session (subscription mode for plans; payment mode for PAYG brief and submission package) and returns the URL.
- POST /billing/webhook: MUST verify signature with STRIPE_WEBHOOK_SECRET. Handle: checkout.session.completed → for plans set plan + current_period_end + reset usage; for PAYG brief create brief_entitlement(source='payg'); for submission package mark it paid/unlock generation. customer.subscription.updated/deleted → update plan/period or set 'free'. NEVER process unsigned webhooks. NEVER log a key.
- Optional customer-portal endpoint for managing/cancelling plans.

PART F — DASHBOARD (Elevated Genera)
- Anonymous search unchanged.
- Brief panel + documents: entitled → full; else 50% preview with a tasteful "Unlock full brief & documents" overlay offering £10 one-off OR a monthly plan.
- "Generate submission package" button: shows the computed price ("Generate package — £300") then routes to Stripe checkout. If payments not configured, show "Coming soon" gracefully (no error).
- Signup/login modal. Free "Get alerts" signup path (no payment).
- If payments not configured, all pay buttons degrade to "Coming soon"; the app stays fully usable.

RULES
- Search stays public/anonymous — never gate it.
- ALL gating server-side; locked content never sent to the unentitled (tests assert locked fields ABSENT from the response, not merely hidden).
- Passwords hashed, never plaintext, never logged. ALL secrets + price IDs from env only — never hardcode, print, log, or commit keys. Webhook signature ALWAYS verified.
- Stripe TEST mode only; app fully usable when keys absent. Dev overrides blocked when TENDER_AGENT_ENV=production.
- Fee amounts/percent/min/max and PREVIEW_FRACTION are named constants — no magic numbers in logic.
- New migration only; existing migrations untouched; alembic reaches head. Build from current main; git pull first (phantom-merge guard).
- Tests (mocked; zero real network/Stripe in CI), 30+ new, all existing green:
  * gate: entitled→full, free/anon→50% preview for BOTH brief and docs (assert locked data absent); downloads blocked for unentitled.
  * metering: plan_100 consumes per new tender, not on re-view; blocks at 100; unlimited never blocks.
  * entitlement: PAYG grants exactly one tender.
  * submission fee: 0.5% with £100/£300 clamp + null→£100; correct dynamic amount sent to Stripe (mocked).
  * webhook: signed events flip plan / create entitlement / unlock package; unsigned rejected.
  * payments-not-configured path; signup/login/password-hash.
- If blocked, "BLOCKED:" at top stating exactly what's needed.

SHIP — OPEN PR, DO NOT MERGE (test on laptop this afternoon)
Commits:
- feat(db): accounts + plans + brief_entitlements + migration
- feat(auth): signup/login (hashed), /me, free-alert signup, dev overrides
- feat(gate): server-enforced 50% teaser on briefs + documents; entitlement checks
- feat(plans): brief-generation metering (100 / unlimited)
- feat(payments): Stripe checkout (plans + PAYG brief + dynamic submission %-fee) + signed webhook (test mode, env-gated, inert without keys)
- feat(dashboard): anonymous search + locked preview + tiered unlock CTAs + package price + free-alert signup
- test: gate + metering + entitlement + fee-calc + webhook + not-configured (all mocked)
PR title: "feat: Phase 4 Chunk 6 — anonymous search + tiered brief access + submission %-fee + Stripe (test mode)"
Description: explain the three lines (free alerts; briefs £10/£100/£250 with 50% teaser; submission package 0.5% clamped £100–£300 upfront), that gating + fee are server-enforced, Stripe is test-mode + inert until keys added, the dev overrides for testing, and the afternoon test steps (incl. a Stripe test card). State at top: "DO NOT MERGE until tested this afternoon." Sentinel: "Phase 4 Chunk 6 — tiered access + Stripe test mode. Ready for review, hold merge."

Begin.
