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

    # Browser bridge (native Windows helper, outside Docker). The container
    # reaches the host at host.docker.internal. The token MUST match the
    # bridge's TENDER_AGENT_BRIDGE_TOKEN (browser-bridge/.env) — that is the
    # canonical env name shared by both sides, so we read it here too (the bare
    # BRIDGE_URL/BRIDGE_TOKEN names stay accepted as a fallback). The download
    # dir is the in-container mount of the host folder the bridge writes to.
    bridge_url: str = Field(
        default="http://host.docker.internal:8765",
        validation_alias=AliasChoices("TENDER_AGENT_BRIDGE_URL", "BRIDGE_URL"),
    )
    bridge_token: str = Field(
        default="",
        validation_alias=AliasChoices("TENDER_AGENT_BRIDGE_TOKEN", "BRIDGE_TOKEN"),
    )
    bridge_download_dir: str = "/app/data/bridge-downloads"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
