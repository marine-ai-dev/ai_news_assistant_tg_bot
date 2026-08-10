"""Typed application configuration.

Everything is read from the environment (optionally via a local ``.env``) with the
``AI_NEWS_`` prefix. Configuration errors fail loudly at startup rather than surfacing
as confusing behaviour later.

No secrets are consumed yet — Phase 1 has no external integrations. When LLM and
Telegram credentials arrive they belong here as ``SecretStr`` fields, never in the
YAML config files and never in the database.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_news_editor.domain.errors import ConfigurationError

LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration for the application."""

    model_config = SettingsConfigDict(
        env_prefix="AI_NEWS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = Field(default="local", description="Free-form deployment label.")
    data_dir: Path = Field(
        default=Path("data"),
        description="Directory for local runtime state. Git-ignored.",
    )
    database_path: Path | None = Field(
        default=None,
        description="SQLite file path. Defaults to <data_dir>/ai_news.sqlite3.",
    )
    sources_config_path: Path = Field(
        default=Path("config/sources.yaml"),
        description="Human-editable source configuration. Committed; contains no secrets.",
    )
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "console"

    auto_publish_enabled: bool = Field(
        default=False,
        description=(
            "Reserved kill-switch for a future optional auto-publish mode. There is no "
            "code path behind it; setting it true is rejected at startup."
        ),
    )

    @field_validator("data_dir", "database_path", "sources_config_path")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @model_validator(mode="after")
    def _reject_auto_publish(self) -> Self:
        """Refuse to start if auto-publish is switched on.

        The flag exists so its absence is explicit and testable. Publishing without a
        human approving the exact draft version is not implemented and must not be
        reachable by flipping a setting.
        """
        if self.auto_publish_enabled:
            raise ValueError(
                "AI_NEWS_AUTO_PUBLISH_ENABLED is not supported: every post requires "
                "explicit human approval of the exact draft version."
            )
        return self

    @property
    def resolved_database_path(self) -> Path:
        """Absolute path of the SQLite file."""
        path = self.database_path or (self.data_dir / "ai_news.sqlite3")
        return path.resolve()

    def ensure_data_dir(self) -> Path:
        """Create the data directory if needed and return it."""
        directory = self.resolved_database_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        return directory


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process, converting validation failures into a fatal error."""
    try:
        return Settings()
    except Exception as exc:
        raise ConfigurationError(f"invalid configuration: {exc}") from exc
