# Accounts the operator must register

Running list of external services / portals that **cannot be set up automatically** —
they require the operator to register, accept terms, verify email, or arrange
2FA. Each phase appends here whenever it discovers a blocker that fits.

| # | Service | URL | Why needed | What it unlocks | Notes |
| - | --- | --- | --- | --- | --- |
| 1 | NEPO (North East Procurement Organisation) | https://www.nepo.org / NEPO portal | Handover §7.3: NEPO is a separate platform — not on ProContract, not EU-Supply — and **needs manual registration** before any automated discovery can be built for it. | North-East England buyer coverage (currently a regional gap: The Chest covers NW, YORtender/EU-Supply covers Yorkshire, NEPO covers NE). | Recorded during Phase 1 (2026-06-11) from the handover's northern-coverage notes. Registration is human-gated per project policy — the operator signs up, then we assess what the logged-in surface exposes before building an adapter. |
| 2 | Email OAuth app registrations (Google / Microsoft) | Google Cloud Console + Azure App registrations | Carried over from the email feature (handover §7.5): one-time OAuth client setup per `docs/email-setup.md`, secrets onto the Azure app. | Inbox watching → tender-email filing → drafted replies (already-built feature, dormant without these). | Not a tender SOURCE, but it is the standing account-shaped blocker list item the handover names. |

## Blocked upstreams — provider-side outages (NOT actionable by us)

These are **not** account/registration blockers and **not** our bugs. The
Phase-1 concurrent-poll fix (#126) reached every source, and the live
`GET /admin/diagnostics/sources-health` readout (operator browser,
2026-06-12T11:26Z) surfaced each one's SPECIFIC upstream error via the
`record_error` path. The fault is on the provider's side; re-check
periodically — no code change recovers these until the provider does.

| Source | Proven error (2026-06-12T11:26Z) | Cause | Our action |
| --- | --- | --- | --- |
| **eTendersNI** | `HTTP 500 Internal Server Error` from `etendersni.gov.uk/...listContractNotices.do?type=atom` | Upstream server fault on their Atom feed endpoint. | Wait for upstream. The adapter already retries (tenacity) and surfaces the 500 cleanly; nothing to fix our side. Re-check next readout. |
| **Sell2Wales** | `SSL: CERTIFICATE_VERIFY_FAILED — certificate has expired` on the Sell2Wales host | The provider's TLS certificate has **expired** (provider-side; affects every correct client). | Wait for them to renew. **Do NOT disable certificate verification as a workaround** — that would silently accept any certificate on a government procurement host (MITM exposure). Recorded as an option the operator may explicitly choose, but the default is wait-for-upstream. Earlier the adapter docstring guessed an HTTP-500 ("nvarchar to float"); the live readout proves the *current* blocker is the expired cert. |

### EU-Supply "Blue Light" tenant — broken host removed (not an account blocker)

The default EU-Supply sweep included `https://bluelight.eu-supply.com`, but
the 2026-06-12T11:26Z readout proved its `/ctm/Supplier/PublicTenders` path
returns **404** — that tenant doesn't expose the public listing at the
standard CTM path (or the host is wrong). It was **removed from the default
`eu_supply_portals`** (#127) so it stops erroring every cycle; the working
EU-Supply tenant(s) keep ingesting (25 tenders in that readout). We don't
guess a replacement URL — if the operator determines the correct Blue Light
tenant host, re-add it to `eu_supply_portals`. Not an account/registration
blocker; logged here only because it was found in the same Phase-1 gate.
