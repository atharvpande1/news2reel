from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEWS2REEL_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./news2reel.db"

    # Set to require this value in an `X-API-Key` header on mutating endpoints.
    # Leave unset only when the server is bound to 127.0.0.1 (see CLAUDE.md's
    # runtime constraints — POST /runs and outbound fetches cost real money).
    api_shared_secret: str | None = None

    # core/net.py fetch limits — every outbound fetch (feed polling, source
    # checks) goes through these caps. See "External input is hostile".
    fetch_connect_timeout_seconds: float = 5.0
    fetch_read_timeout_seconds: float = 10.0
    fetch_max_response_bytes: int = 5_000_000
    fetch_max_redirects: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
