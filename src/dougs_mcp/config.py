"""Typed configuration loaded from environment variables (DOUGS_*) or a .env file."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server settings loaded from process env then ``.env`` (if present)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    dougs_email: str = Field(..., description="Dougs account email used to log in.")
    dougs_password: SecretStr = Field(..., description="Dougs account password.")
    dougs_totp_secret: SecretStr | None = Field(
        None,
        description=(
            "Authenticator-app secret (base32, or the otpauth:// URI) to pass Dougs' MFA "
            "on login. Required when the account has MFA enabled."
        ),
    )
    dougs_base_url: str = Field("https://app.dougs.fr", description="Dougs API base URL.")
    dougs_company_id: int | None = Field(
        None,
        description="Optional pinned company id; falls back to the user's preferred company.",
    )
    dougs_upload_root: Path | None = Field(
        None,
        description=(
            "Directory add_attachment and upload_vendor_invoice may read. Unset refuses every "
            "upload."
        ),
    )


def load_settings() -> Settings:
    return Settings()
