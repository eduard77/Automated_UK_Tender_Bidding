from functools import lru_cache

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

    # Document downloader
    document_storage_dir: str = "/var/tender-agent/documents"
    document_max_bytes: int = 50 * 1024 * 1024  # 50 MB

    http_timeout_seconds: int = 30
    http_user_agent: str = "tender-agent/0.1 (compliance-research)"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
