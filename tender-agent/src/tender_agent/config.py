from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://tender:tender@localhost:5432/tender_agent"

    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # CORS — explicit allow-list of dashboard origins. The defaults cover
    # local-dev (Next.js on :3000) so a fresh checkout works out of the box.
    # In any non-dev deployment, set CORS_ALLOW_ORIGINS to the deployed
    # dashboard origin(s); NEVER use "*" — browsers reject wildcard origins
    # on credentialed requests, and the dashboard sends the session cookie
    # via credentials: "include".
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
    )

    poll_interval_minutes: int = 30
    lookback_days_initial: int = 7

    fts_api_base: str = "https://www.find-tender.service.gov.uk/api/1.0"
    contracts_finder_api_base: str = (
        "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS"
    )
    pcs_api_base: str = "https://api.publiccontractsscotland.gov.uk/v1"
    sell2wales_api_base: str = "https://api.sell2wales.gov.wales/v1"
    etendersni_feed_url: str = "https://etendersni.gov.uk/epps/cft/listContractNotices.do?type=atom"

    # Document downloader. A single, writable, host-mounted location under
    # /app/data (see docker-compose.yml). Every document/staging path derives
    # from this — never a bare relative 'data' path.
    document_storage_dir: str = "/app/data/documents"
    document_max_bytes: int = 50 * 1024 * 1024  # 50 MB

    # --- Local-fetch pivot: cloud ingest (DB + Blob) -------------------------
    # Delta refuses cloud/datacenter IPs (confirmed 403), so Delta is fetched on
    # the operator's own machine and the results are pushed UP to this backend's
    # ingest endpoint, which stores the bytes in Azure Blob.
    #
    # Azure Blob via MANAGED IDENTITY (no connection string): when
    # `azure_storage_account` is set, ingested document bytes go to
    # https://<account>.blob.core.windows.net/<container>/<tender_id>/<sha256>.<ext>.
    # DefaultAzureCredential resolves the App Service's system-assigned identity
    # in Azure (granted "Storage Blob Data Contributor" on the account) and dev
    # creds locally. Empty account → falls back to the local-disk backend so a
    # plain checkout / CI needs no Azure.
    azure_storage_account: str = ""  # e.g. "generasystemsfiles"
    azure_blob_container: str = "tender-documents"
    # Per-document size ceiling for ingest (mirrors the Delta adapter's 100 MB
    # MAX_FILE_BYTES — Delta docs can exceed the 50 MB HTTP-download cap).
    ingest_max_bytes: int = 100 * 1024 * 1024  # 100 MB

    # The cloud backend must NOT initiate fetches for these platforms — they
    # require a local human login from a residential IP (the cloud IP is 403'd),
    # so a cloud attempt would only contend with the operator's local session.
    # The local fetch runner (scripts/fetch_delta.py) sets `local_fetch_runner`
    # true in its OWN process to bypass this and drive the browser locally.
    local_fetch_platforms: list[str] = Field(
        default_factory=lambda: ["delta_esourcing"],
    )
    local_fetch_runner: bool = False

    http_timeout_seconds: int = 30
    http_user_agent: str = "tender-agent/0.1 (compliance-research)"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    # Web Push (VAPID). Generate a keypair with `npm run generate-vapid` in the
    # dashboard. Public key is also exposed via GET /push/vapid-public-key so the
    # dashboard doesn't need to bake it into its build. If unset, push endpoints
    # return 503 and ingestion skips dispatch (logs a structured event).
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@example.com"

    # Dashboard origin — used as the base for notification `url` paths.
    dashboard_base_url: str = "http://localhost:3000"

    # Operator account for SYSTEM/operational push alerts (e.g. "log in to fetch
    # documents" from the portal orchestrator). These aren't tied to a specific
    # end user, so they target this account's devices only. Unset → such alerts
    # notify NOBODY (logged), never a cross-account broadcast.
    push_operator_account_id: int | None = None

    # Browser bridge. There are TWO modes (Delta cloud stage 2):
    #   1. In-process Playwright (default in cloud) — `bridge_url=""`. The
    #      backend drives Chromium directly via `bridge_in_process.py`. No
    #      separate service, no shared volume, no HTTP boundary.
    #   2. HTTP to a standalone bridge — set `TENDER_AGENT_BRIDGE_URL` to
    #      something like `http://host.docker.internal:8765`. Existing local
    #      Windows dev users with the standalone bridge running keep that
    #      workflow; the orchestrator surface is identical either way.
    # `bridge_url` defaults EMPTY so the cloud deployment lands in-process
    # without needing extra env vars; `make_bridge_client()` picks the
    # implementation based on this value. The token only matters in HTTP mode.
    bridge_url: str = Field(
        default="",
        validation_alias=AliasChoices("TENDER_AGENT_BRIDGE_URL", "BRIDGE_URL"),
    )
    bridge_token: str = Field(
        default="",
        validation_alias=AliasChoices("TENDER_AGENT_BRIDGE_TOKEN", "BRIDGE_TOKEN"),
    )
    # Where Chromium writes captured downloads. In HTTP mode this used to be
    # the in-container mount of the host folder the bridge wrote into; in the
    # in-process mode the backend writes and reads the same dir itself, so the
    # default just needs to be writable.
    bridge_download_dir: str = "/app/data/bridge-downloads"
    # Persistent Playwright `user_data_dir` for the in-process driver. Stage 3
    # will drop an operator-exported `storage_state.json` for a given slug
    # under `<bridge_state_dir>/<slug>/` so Delta picks up the session without
    # the user typing a password.
    bridge_state_dir: str = "/app/data/bridge-sessions"

    # --- Proactis discovery (Phase 4 chunk 9) -------------------------------
    # Filters applied on the Find Opportunities listing for the SCHEDULED
    # discovery job + the admin manual trigger. Step 2 will wire the
    # dashboard's filter page to populate these (or a per-user variant);
    # for Step 1 they're env-driven. Defaults are intentionally
    # unconstrained-but-open-only so an unset config still does useful work:
    # walks open opportunities only (include_closed=False).
    proactis_discovery_enabled: bool = False
    proactis_discovery_interval_minutes: int = 60
    proactis_discovery_keywords: str = ""
    proactis_discovery_regions: list[str] = Field(default_factory=list)
    proactis_discovery_categories: list[str] = Field(default_factory=list)
    proactis_discovery_portals: list[str] = Field(default_factory=list)
    proactis_discovery_organisations: list[str] = Field(default_factory=list)
    proactis_discovery_include_closed: bool = False
    proactis_discovery_max_pages: int = 20

    # --- Phase 4 chunk 6: accounts, Stripe, gating ---------------------------
    # Hard env switch — when set to 'production', the dev override endpoints
    # refuse to grant entitlements or rewrite plan tiers. Local/dev defaults
    # to '' (any non-production value) which keeps the override usable.
    tender_agent_env: str = ""

    # Stripe (test mode). When ANY of these is empty, the billing endpoints
    # return a clean "payments_not_configured" response and the rest of the
    # app stays fully usable (gates run off plan + entitlement + dev flag).
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""

    # Pre-created Stripe prices (test mode). These are the IDs handed over for
    # this build; amounts live in the Stripe dashboard, not in our code.
    stripe_price_brief_payg: str = "price_1TdURiDFsiLtuBZSzRPop0Rr"
    stripe_price_plan_100: str = "price_1TdUSTDFsiLtuBZSKmhzgXfy"
    stripe_price_plan_unlim: str = "price_1TdUSmDFsiLtuBZSihjbMA8k"

    # Where Stripe Checkout should redirect after a successful or cancelled
    # session. Public dashboard URL; defaults reuse dashboard_base_url.
    stripe_success_url: str = ""
    stripe_cancel_url: str = ""

    # Session cookie name + lifetime. The cookie value is opaque — looked up
    # in account_sessions, NOT a signed JWT — so revocation is just a DELETE.
    session_cookie_name: str = "tender_agent_session"
    session_lifetime_days: int = 30

    # --- Email integration (Phase 6 §5.8) -----------------------------------
    # Connect an inbox over OAuth (delegated READ-ONLY access — never stored
    # passwords), watch for tender emails by exact subject reference, file
    # attachments via the existing ingest path, draft a reply, and notify.
    #
    # The provider app registrations (Google Cloud OAuth client, Azure app,
    # Yahoo app) are a ONE-TIME human setup the operator does with their own
    # provider access — Claude Code cannot do them. The code reads the client
    # id/secret + redirect URI from these settings; until they are set, the
    # connect flow fails with a clear "provider not configured yet" message.
    # See the PR description / docs/email_setup.md for the exact checklists.
    email_poll_enabled: bool = False
    email_poll_interval_minutes: int = 5
    # How far back (minutes) to overlap each incremental poll so a message that
    # arrives mid-cycle is never skipped. Idempotency (unique provider message
    # id) makes the overlap safe.
    email_poll_overlap_minutes: int = 10
    # On a mailbox's first poll (no watermark), how far back to look.
    email_initial_lookback_days: int = 1
    # Safety cap on messages fetched per mailbox per poll.
    email_poll_max_messages: int = 50

    # The provider redirects back here after consent. Must EXACTLY match the
    # redirect URI registered with each provider, e.g.
    # https://api.yourdomain/email/oauth/callback
    email_oauth_redirect_uri: str = ""

    # Google / Gmail (Gmail API, scope gmail.readonly).
    gmail_client_id: str = ""
    gmail_client_secret: str = ""

    # Microsoft / Outlook (Microsoft Graph, scopes Mail.Read offline_access
    # User.Read). "common" lets both work + personal Microsoft accounts sign in.
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_tenant: str = "common"

    # Yahoo (DEFERRED — see PR). Yahoo has no clean read-only mail REST API;
    # it requires OAuth2 + IMAP. The provider slot exists behind the common
    # interface and reports "not yet configured" until these are wired.
    yahoo_client_id: str = ""
    yahoo_client_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
