# Accounts the operator must register

Running list of external services / portals that **cannot be set up automatically** —
they require the operator to register, accept terms, verify email, or arrange
2FA. Each phase appends here whenever it discovers a blocker that fits.

| # | Service | URL | Why needed | What it unlocks | Notes |
| - | --- | --- | --- | --- | --- |
| 1 | NEPO (North East Procurement Organisation) | https://www.nepo.org / NEPO portal | Handover §7.3: NEPO is a separate platform — not on ProContract, not EU-Supply — and **needs manual registration** before any automated discovery can be built for it. | North-East England buyer coverage (currently a regional gap: The Chest covers NW, YORtender/EU-Supply covers Yorkshire, NEPO covers NE). | Recorded during Phase 1 (2026-06-11) from the handover's northern-coverage notes. Registration is human-gated per project policy — the operator signs up, then we assess what the logged-in surface exposes before building an adapter. |
| 2 | Email OAuth app registrations (Google / Microsoft) | Google Cloud Console + Azure App registrations | Carried over from the email feature (handover §7.5): one-time OAuth client setup per `docs/email-setup.md`, secrets onto the Azure app. | Inbox watching → tender-email filing → drafted replies (already-built feature, dormant without these). | Not a tender SOURCE, but it is the standing account-shaped blocker list item the handover names. |

## Pending — upstream-blocked candidates (re-check after next health readout)

These are **not** account/registration blockers — they're upstream-service
failures the silent-sources diagnosis surfaced but cannot be classified
without the live error string the new sources-health row will now carry:

- **Sell2Wales** — adapter docstring documents an upstream `HTTP 500
  "Error converting data type nvarchar to float"` from their backend SQL
  layer. The Phase-1-continuation PR surfaces that exact error string
  on the next PollRun. If the readout confirms 500, this becomes a
  BLOCKED-UPSTREAM (their API; nothing we can fix); if it shows a 403,
  it's an Azure-egress problem and we route via the disguised bridge.
  Either branch is decided by the next `GET /admin/diagnostics/sources-
  health` after PR merges.
- **eTendersNI** — the same generic-error story before this PR. The new
  PollRun row will name the specific httpx.HTTPError class + URL it failed
  on; same branch logic.

Both will be promoted to a real row in this table or removed entirely
once that readout lands.
