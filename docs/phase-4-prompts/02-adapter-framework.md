Task: build the Universal Portal Adapter Framework with platform-family awareness. Autonomous end-to-end run. The user is going to work and unavailable.

This is prompt 2 of 12 in the Phase 4 build sequence. PR #32 (Discovery + Registry) merged to main. 1,571 portals classified; ~50 real procurement portals identified across 7 distinct platform families.

Required reading at the start:
- docs/phase-4-design.md (full design)
- docs/phase-4-prompts/01-discovery-and-registry.md (what just shipped)

================================================================
KEY INSIGHT THAT SHAPES THIS PROMPT
================================================================

Classification revealed: the UK procurement landscape isn't 1,571 unique portals. It's roughly 7 platform families with many buyer-specific instances:

1. Delta eSourcing — delta-esourcing.com + *.delta-esourcing.com subdomains (Tees Valley, NEUPC, London Barnet, eTenderWales, etc.)
2. JAGGAER — *.app.jaggaer.com (Home Office, DfT, eTenderWales)
3. BravoSolution (now Jaggaer) — *.bravosolution.com (Turner & Townsend, eTenderWales legacy)
4. ProContract / Due North — procontract.due-north.com + sebp.due-north.com + custom-branded regional portals (East Midlands)
5. In-Tend — in-tendhost.co.uk + sell2.in-tend.co.uk + buyer paths
6. Proactis — supplierlive.proactisp2p.com + buyer-branded P2P instances
7. myTenders / Milstream — mytenders.co.uk

Building ONE adapter per platform unlocks ALL buyers using that platform. This is the architectural foundation.

================================================================
PART A — SCHEMA: PORTAL PLATFORMS
================================================================

Branch: feat-adapter-framework

1. Create migration `0006_portal_platforms.py`:

   Table `portal_platforms`:
   - id (bigserial PK)
   - slug (text unique not null) — e.g. "delta_esourcing", "jaggaer", "procontract"
   - display_name (text not null) — e.g. "Delta eSourcing", "JAGGAER", "ProContract (Due North)"
   - vendor (text nullable) — e.g. "BiP Solutions", "JAGGAER Inc", "Proactis"
   - domain_patterns (jsonb not null default '[]') — list of regex strings matching domains belonging to this platform (e.g. for Delta: ["^delta-esourcing\\.com$", "^[a-z0-9-]+\\.delta-esourcing\\.com$"])
   - adapter_module (text nullable) — Python module path for the adapter when built
   - adapter_status (text not null default 'not_started') — same enum as portals
   - login_type (text not null default 'unknown') — same enum as portals
   - notes (text nullable) — human notes
   - first_observed_buyer_count (integer not null default 0) — how many distinct buyer instances we've seen on this platform
   - total_tender_count (integer not null default 0) — sum of tender_count across all portals matched to this platform
   - created_at, updated_at

   Indexes:
   - UNIQUE INDEX on (slug)
   - INDEX on (adapter_status)

   Seed with the 7 known platforms (full list in Part B).

2. Add column to existing `portals` table:
   - `platform_id` (bigint nullable, FK to portal_platforms.id ON DELETE SET NULL)
   - INDEX on platform_id
   - Backfill in the same migration: for every existing portal, try to match its domain against each platform's domain_patterns. If matched, set platform_id.

3. Add column to existing `portals` table:
   - `is_email_domain` (boolean not null default false)
   - Backfill: any portal where ALL recent sightings have sighting_type='contact_email' OR url starts with 'mailto:' → set is_email_domain=true
   - This is the long-overdue fix for nhs.net etc.

4. SQLAlchemy ORM model `PortalPlatform` in tender_agent/db/models.py. Add `Portal.platform` relationship.

5. Pydantic schemas in tender_agent/api/schemas/portals.py for PortalPlatform.

================================================================
PART B — PLATFORM SEED DATA
================================================================

6. The migration seeds these 7 platforms. Use exact slugs as shown:

   slug: delta_esourcing
   display_name: Delta eSourcing
   vendor: BiP Solutions
   domain_patterns: ["^delta-esourcing\\.com$", "^[a-z0-9-]+\\.delta-esourcing\\.com$"]
   login_type: username_password
   notes: Multi-tenant procurement platform. Many UK consortia (Tees Valley, NEUPC, London Barnet, etc.) and Welsh national procurement.

   slug: jaggaer
   display_name: JAGGAER eSourcing
   vendor: JAGGAER Inc (formerly BravoSolution)
   domain_patterns: ["^[a-z0-9-]+\\.app\\.jaggaer\\.com$", "^[a-z0-9-]+\\.ukp\\.app\\.jaggaer\\.com$"]
   login_type: username_password
   notes: Used by central UK government departments. Multi-tenant SaaS with department subdomains.

   slug: bravosolution
   display_name: BravoSolution (legacy)
   vendor: JAGGAER Inc
   domain_patterns: ["^[a-z0-9-]+\\.bravosolution\\.com$", "^[a-z0-9-]+\\.bravosolution\\.co\\.uk$"]
   login_type: username_password
   notes: Legacy BravoSolution domains, now operated by JAGGAER. eTenderWales legacy URL plus consultancy/client portals.

   slug: procontract
   display_name: ProContract (Due North)
   vendor: Proactis (Due North)
   domain_patterns: ["^procontract\\.due-north\\.com$", "^[a-z0-9-]+\\.due-north\\.com$", "^eastmidstenders\\.org$"]
   login_type: username_password
   notes: Major UK local authority platform. Note custom-branded portals exist outside the .due-north.com domain (e.g. eastmidstenders.org).

   slug: in_tend
   display_name: In-Tend
   vendor: In-Tend Ltd
   domain_patterns: ["^in-tendhost\\.co\\.uk$", "^[a-z0-9-]+\\.in-tend\\.co\\.uk$", "^in-tend\\.co\\.uk$"]
   login_type: username_password
   notes: UK procurement platform serving local authorities. Multi-tenant with buyer-specific paths.

   slug: proactis
   display_name: Proactis Supplier Network
   vendor: Proactis Holdings PLC
   domain_patterns: ["^[a-z0-9-]+\\.proactisp2p\\.com$", "^supplierlive\\.proactisp2p\\.com$"]
   login_type: username_password
   notes: P2P platform used by many UK public sector buyers. Supplier-side portal at supplierlive.

   slug: mytenders
   display_name: myTenders
   vendor: Milstream Associates
   domain_patterns: ["^mytenders\\.co\\.uk$", "^www\\.mytenders\\.co\\.uk$", "^[a-z0-9-]+\\.litmustms\\.co\\.uk$", "^litmustms\\.co\\.uk$"]
   login_type: username_password
   notes: ASP.NET-based UK procurement portal. Litmus TMS appears related.

   ALSO seed these as platforms with adapter_status='read_only' from day one (no login needed, public API/HTML):

   slug: contracts_finder_direct
   display_name: Contracts Finder direct documents
   vendor: UK Government
   domain_patterns: ["^assets\\.publishing\\.service\\.gov\\.uk$"]
   login_type: none
   notes: Direct document URLs from CF tender records. Public, no auth required.

   slug: ted_eu
   display_name: TED (Tenders Electronic Daily)
   vendor: European Commission
   domain_patterns: ["^ted\\.europa\\.eu$"]
   login_type: oauth
   notes: EU procurement publication portal.

   slug: crown_commercial
   display_name: Crown Commercial Service / GCA
   vendor: UK Government Commercial Agency
   domain_patterns: ["^crowncommercial\\.gov\\.uk$", "^www\\.crowncommercial\\.gov\\.uk$", "^gca\\.gov\\.uk$", "^www\\.gca\\.gov\\.uk$"]
   login_type: username_password
   notes: National framework agreements.

================================================================
PART C — THE ADAPTER ABSTRACTION
================================================================

7. Create `tender_agent/adapters/portals/base.py`:

```python
   from abc import ABC, abstractmethod
   from dataclasses import dataclass
   from typing import Literal, Optional
   from pathlib import Path
   
   class LoginType(StrEnum): ...  # match existing enum
   
   @dataclass
   class TenderReference:
       """Identifies a specific tender on a portal."""
       url: Optional[str]       # full URL if we have one
       reference_text: Optional[str]  # e.g. "FA0001/24-25" when CF doesn't give a URL
       portal_id: int
   
   @dataclass
   class Credentials:
       username: Optional[str]
       password: Optional[str]
       email: Optional[str]
       extra: dict  # platform-specific (e.g. {"company_registration": "12345678"})
   
   @dataclass
   class AuthResult:
       status: Literal['success', 'needs_registration', 'invalid_credentials',
                       'requires_2fa', 'blocked', 'error']
       detail: str
       session_token: Optional[str] = None
   
   @dataclass
   class LocateResult:
       status: Literal['found', 'not_found', 'requires_interest_first',
                       'access_denied', 'error']
       tender_page_url: Optional[str]
       detail: str
   
   @dataclass
   class RegisterResult:
       status: Literal['registered', 'already_registered',
                       'requires_email_confirmation', 'requires_buyer_approval',
                       'rejected', 'error']
       detail: str
       confirmation_email_expected: bool = False
   
   @dataclass
   class DownloadedFile:
       filename: str
       path: Path
       mime_type: str
       size_bytes: int
       source_url: str
   
   @dataclass
   class DownloadResult:
       status: Literal['complete', 'partial', 'requires_email_documents',
                       'access_denied', 'error']
       files: list[DownloadedFile]
       missing: list[str]
       detail: str
   
   @dataclass
   class HealthCheckResult:
       status: Literal['healthy', 'degraded', 'broken']
       detail: str
       checked_at: datetime
   
   class PortalAdapter(ABC):
       """Base class for every portal adapter.
       
       Adapters are PLATFORM-LEVEL, not buyer-level. One Delta adapter
       handles delta-esourcing.com AND teesvalley.delta-esourcing.com AND
       neupc.delta-esourcing.com — same code, different config.
       """
       platform_slug: str  # must match a portal_platforms.slug
       
       @classmethod
       @abstractmethod
       def matches_url(cls, url: str) -> bool:
           """Return True if this adapter handles the given URL.
           
           Default impl uses portal_platforms.domain_patterns from DB.
           Subclasses may override for special cases (e.g. eastmidstenders.org
           is ProContract but on a custom domain).
           """
       
       @abstractmethod
       async def authenticate(self, ctx, creds: Credentials) -> AuthResult: ...
       
       @abstractmethod
       async def locate_tender(self, ctx, tender_ref: TenderReference) -> LocateResult: ...
       
       @abstractmethod
       async def register_interest(self, ctx) -> RegisterResult: ...
       
       @abstractmethod
       async def download_documents(self, ctx, dest_dir: Path) -> DownloadResult: ...
       
       async def screenshot(self, ctx, label: str) -> Path:
           """Default: capture screenshot to dest_dir / {label}.png"""
       
       async def health_check(self) -> HealthCheckResult:
           """Default: GET platform's first domain, verify 200 + expected element."""
```

8. Implement `FallbackAdapter` in `tender_agent/adapters/portals/fallback.py`:

   For portals with no specific adapter. Behaviour:
   - `matches_url`: never (it's the explicit last resort, called by orchestrator)
   - `authenticate`: always returns `AuthResult(status='success', detail='no auth attempted')` — anonymous mode
   - `locate_tender`: navigates to the URL, screenshots, returns `found` if 200 response
   - `register_interest`: returns `RegisterResult(status='error', detail='Fallback adapter cannot register interest. Manual registration required.')`
   - `download_documents`: parses the loaded page for visible PDF/DOCX links, downloads each. Returns `partial` with `missing=['authenticated documents']`.

================================================================
PART D — BROWSER CONTEXT MANAGEMENT
================================================================

9. Create `tender_agent/adapters/browser.py`:

```python
   class BrowserContextManager:
       """Manages persistent Playwright contexts per (user_id, portal_id).
       
       Storage: ~/.tender-agent/browsers/{user_id}/{platform_slug}/
       Cookies + local storage persist across runs.
       Default headless. Use TENDER_AGENT_BROWSER_HEADED=1 to override.
       """
       
       async def get_context(self, user_id: str, platform_slug: str): ...
       async def close_context(self, user_id: str, platform_slug: str): ...
       async def screenshot(self, ctx, name: str, dest: Path): ...
```

   Use playwright-stealth plugin. Realistic user-agent. Default viewport 1440×900. Anti-bot baseline applied to every context creation.

   Storage paths must be configurable via env var TENDER_AGENT_BROWSER_STATE_DIR (defaults to ~/.tender-agent/browsers).

10. Add `playwright` and `playwright-stealth` to pyproject.toml dependencies. Update Dockerfile to install Playwright browsers during build: `RUN playwright install chromium && playwright install-deps`.

================================================================
PART E — CREDENTIALS VAULT
================================================================

11. Create `tender_agent/credentials/store.py`:

    Storage backend: SQLite at ~/.tender-agent/credentials.db, encrypted with sqlcipher. Encryption key stored in OS keyring (use `keyring` library — supports Windows Credential Manager, macOS Keychain, Linux Secret Service automatically).

    Table inside the encrypted SQLite:
