from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _secret(value: str, path: Path | None) -> str:
    if value:
        return value.strip()
    if path and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCHIVE_",
        env_file=".env.vps",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: Path = Path("/srv/archive")
    legacy_zip: Path = Path("/var/cache/archive/syrian-archive-site.zip")
    database_url: str = ""
    database_host: str = "postgres"
    database_port: int = 5432
    database_name: str = "archive_admin"
    database_user: str = "archive"
    database_password: str = ""
    database_password_file: Path | None = Path("/run/secrets/postgres_password")
    redis_url: str = "redis://redis:6379/0"

    admin_username: str = "admin"
    admin_password_hash: str = ""
    admin_password_hash_file: Path | None = Path("/run/secrets/admin_password_hash")
    session_secret: str = ""
    session_secret_file: Path | None = Path("/run/secrets/session_secret")
    secure_cookies: bool = True

    collector_delay: float = Field(default=0.75, ge=0)
    collector_timeout: float = Field(default=10.0, gt=0)
    collector_retries: int = Field(default=1, ge=0, le=5)
    collector_workers: int = Field(default=6, ge=1, le=32)
    collector_per_host_workers: int = Field(default=1, ge=1, le=4)
    incident_chunk_size: int = Field(default=10, ge=1, le=100)
    source_chunk_size: int = Field(default=100, ge=1, le=1000)

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        password = _secret(self.database_password, self.database_password_file)
        return (
            f"postgresql+psycopg://{self.database_user}:{password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def resolved_admin_password_hash(self) -> str:
        return _secret(self.admin_password_hash, self.admin_password_hash_file)

    @property
    def resolved_session_secret(self) -> str:
        return _secret(self.session_secret, self.session_secret_file)

    @model_validator(mode="after")
    def validate_worker_limits(self) -> Settings:
        if self.collector_per_host_workers > self.collector_workers:
            raise ValueError(
                "collector_per_host_workers cannot exceed collector_workers"
            )
        return self

    def require_web_secrets(self) -> None:
        if not self.resolved_admin_password_hash:
            raise RuntimeError(
                "ARCHIVE_ADMIN_PASSWORD_HASH or its Docker secret is required"
            )
        if len(self.resolved_session_secret) < 32:
            raise RuntimeError("session secret must contain at least 32 characters")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
