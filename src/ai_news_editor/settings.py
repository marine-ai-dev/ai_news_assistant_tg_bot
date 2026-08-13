"""Typed application configuration.

Everything is read from the environment (optionally via a local ``.env``) with the
``AI_NEWS_`` prefix. Configuration errors fail loudly at startup rather than surfacing
as confusing behaviour later.

The only secret this application has is the Telegram bot token. It is a ``SecretStr``,
so printing the settings object cannot leak it; it lives in the environment, never in
the YAML config files and never in the database.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
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

    telegram_bot_token: SecretStr | None = Field(
        default=None,
        description=(
            "Bot token from BotFather. SecretStr so it cannot be printed by accident — "
            "repr and str both mask it. Never written to the database or a log."
        ),
    )
    telegram_channel: str | None = Field(
        default=None,
        description=(
            "Publication destination: a public @username or a numeric chat id "
            "(channels are typically negative, e.g. -1001234567890)."
        ),
    )

    telegram_owner_user_id: int | None = Field(
        default=None,
        description=(
            "The single Telegram user allowed to use the review bot. Not a secret, but "
            "not committed either: it identifies a real account. Discover it with "
            "'ai-news telegram whoami'."
        ),
    )

    media_dir: Path = Field(
        default=Path("media"),
        description=(
            "Where post images and files live. Every approved asset is a path relative "
            "to this directory; nothing outside it can be published."
        ),
    )
    channel_handle: str = Field(
        default="@learn_ai_easy",
        description=(
            "The public channel handle used in the forwarding call-to-action. Configured "
            "rather than written into prose, so a writing session cannot mistype it."
        ),
    )
    channel_footer_enabled: bool = Field(
        default=True,
        description="Whether new drafts close with the invite-a-friend call-to-action.",
    )
    channel_footer_text: str = Field(
        default="Запросити друзів",
        description="The call-to-action wording. The leading emoji varies per post.",
    )

    @field_validator(
        "telegram_bot_token", "telegram_channel", "telegram_owner_user_id", mode="before"
    )
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """Treat ``NAME=`` in a .env file as "not configured" rather than as a value.

        Writing a bare key with nothing after it is the natural way to say "I have not
        filled this in yet", and it is exactly what ``.env.example`` ships. Without this,
        an empty owner id reaches Pydantic as ``""`` and fails integer parsing, so a
        fresh clone that follows the README breaks on the first command with a
        validation error about a setting the reader never set.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("telegram_channel")
    @classmethod
    def _check_channel(cls, value: str | None) -> str | None:
        """Catch a destination that Telegram will certainly reject, at startup.

        A channel configured as bare text ("my_channel") silently resolves to nothing
        useful; better to say so before a human is standing at a publish prompt.
        """
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if trimmed.startswith("@"):
            if len(trimmed) < 2:
                raise ValueError("AI_NEWS_TELEGRAM_CHANNEL is just '@'")
            return trimmed
        try:
            int(trimmed)
        except ValueError:
            raise ValueError(
                "AI_NEWS_TELEGRAM_CHANNEL must be a public @username or a numeric chat "
                f"id, got {trimmed!r}"
            ) from None
        return trimmed

    @field_validator("data_dir", "database_path", "sources_config_path", "media_dir")
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

    @property
    def resolved_media_dir(self) -> Path:
        return self.media_dir.resolve()

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
