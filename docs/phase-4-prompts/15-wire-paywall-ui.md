TASK: Wire the existing paywall UI into the running dashboard (last-mile front-end only)

GOAL (plain English)
The backend paywall is already enforced server-side and the components already exist (components/UnlockOverlay.tsx, components/SignInModal.tsx) — they are just not mounted, fetchMe() is never called, and BriefComplete ignores the brief_json.locked flag. Wire them up so an end-user actually SEES the funnel: a half-brief shows a lock state + unlock CTA, there's a sign-in/sign-up entry point, and logged-in/entitled users see the full brief. FRONT-END ONLY. Do NOT change any backend gating, billing, or redaction logic — it is correct and must stay the single source of truth.

DO NOT TOUCH
- Any backend file (tender-agent/**). No changes to api/tender_brief.py, billing, entitlement, or redaction.
- The server-side gating contract: the client must TRUST the backend. Never try to reconstruct hidden fields client-side. The client only reads what the backend returns + the locked marker; it never bypasses redaction.

BUILD (tender-agent-dashboard only)
1. Auth state: call fetchMe() on app load (a context/provider or top-level hook) so the app knows the caller's plan + entitlement. Anonymous is the default; no errors when not logged in.
2. Header: add a "Sign in" entry point to the app shell/header that opens SignInModal. When logged in, show account state (email + plan) and a logout action. SignInModal already supports login / signup / free-alerts — just mount it.
3. TenderDetail / BriefComplete (components/TenderDetail.tsx:639-650): read brief_json.locked. When locked=true (anonymous/unentitled), render the half-brief WITH a clear "first half / preview" indication and mount UnlockOverlay (the PAYG £10 / Plan 100 / Unlimited + submission-package CTA). When the user is entitled (locked not set / full brief returned), render the full brief as now, no overlay.
4. UnlockOverlay is already wired to startCheckout → POST /billing/checkout → redirect to the Stripe URL; just mount it and pass the tender_id. It already degrades to "(coming soon)" when payments_configured is false — keep that.
5. Submission-package CTA: surface the computed fee via fetchSubmissionFee(tenderId) where the overlay shows it (already typed in lib/api.ts).
6. After a successful login/signup, re-run fetchMe() and re-fetch the brief so a now-entitled user sees the full version without a manual refresh.

RULES
- Front-end only; zero backend changes. Server stays the source of truth for what content is returned.
- Don't add a Stripe publishable key or Stripe.js — the flow is redirect-to-hosted-Checkout (confirmed); no client key needed.
- Reuse existing typed helpers in lib/api.ts (fetchMe, signup, login, logout, freeAlertsSignup, fetchBillingStatus, fetchSubmissionFee, startCheckout). Do not write new API plumbing.
- Match the existing "Elevated Genera" dashboard styling; the lock state and overlay should look intentional, not bolted on.
- Build from current main; git pull first. Existing front-end build/lint must stay green. Add/adjust front-end tests where the project already has them.
- Handle the not-logged-in and payments-not-configured states gracefully everywhere (no crashes, no dead buttons — "coming soon" instead).

VERIFY
- Anonymous user opens a tender with a locked brief → sees half + lock indicator + UnlockOverlay with the three plan buttons and submission-package fee.
- Sign-in button opens SignInModal; signup/login works against the existing endpoints; after login fetchMe re-runs and an entitled user sees the full brief.
- Payments-not-configured → buttons show "coming soon", no errors.

SHIP — OPEN PR, DO NOT MERGE
Commits:
- feat(dashboard): auth context via fetchMe + header sign-in/account state
- feat(dashboard): mount SignInModal (login / signup / free alerts)
- feat(dashboard): render brief_json.locked — half-brief lock state + mount UnlockOverlay
- feat(dashboard): submission-package fee surfaced in overlay
PR title: "feat(dashboard): wire paywall UI — lock state, unlock overlay, sign-in (front-end last mile)"
Description: explain backend gating is unchanged and remains source of truth; this only mounts existing components, reads the locked flag, and adds the sign-in entry point. Note it depends on the chunk-6 backend (PR #11) being run locally to function end-to-end. Sentinel: "Front-end last mile — no backend changes, server stays source of truth."

Begin.
