from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All secrets come from the Render environment. Nothing here is ever sent to a browser."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str  # Supabase Postgres connection string (service role / pooler)
    render_api_key: str = ""
    render_api_base: str = "https://api.render.com/v1"

    # Owner auth (Supabase). Either the legacy HS256 JWT secret, or the project URL for JWKS.
    supabase_url: str = ""
    supabase_jwt_secret: str = ""
    owner_emails: list[str] = Field(default_factory=list)  # e.g. ["me@example.com"]; DB `owners` table also checked

    # HMAC key for hashing visitor ids so no raw IP/session id is stored.
    visitor_hash_key: str

    # Shared secret for the budget-enforcement cron (Render Cron / GitHub Actions).
    cron_secret: str = ""

    # CORS: the portfolio + dashboard origins.
    cors_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
