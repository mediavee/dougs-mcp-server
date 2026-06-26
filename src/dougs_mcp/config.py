"""Configuration loaded from environment variables (prefix DOUGS_) or a .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOUGS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    email: str
    password: str
    base_url: str = "https://app.dougs.fr"
    # Optional pinned company id; falls back to the user's preferred company.
    company_id: int | None = None
