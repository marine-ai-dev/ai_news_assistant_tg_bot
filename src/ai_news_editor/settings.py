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
from typing import ClassVar, Literal, Self

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
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

    test_channel: str | None = Field(
        default=None,
        description=(
            "A second destination, separate from telegram_channel, for a real send "
            "nobody but the owner reads. Same validation and the same publications "
            "table — a channel is just a string this application sends to, so no "
            "schema change was needed to keep test and production history apart."
        ),
    )

    # -- GitHub Actions NEWS automation ---------------------------------------
    #
    # A second, narrower kill switch from auto_publish_enabled above. That flag is a
    # blanket rejection with no code path behind it at all — it exists so a human
    # publish flow can never be made unattended by flipping a setting. This one gates a
    # deliberately built, narrower thing: NEWS only, sourced only from configured
    # OFFICIAL sources, approved under a distinguishable actor, subject to a daily cap.
    # The two flags are unrelated on purpose; this module never touches the other one.
    automation_enabled: bool = Field(
        default=False,
        description=(
            "Kill switch for unattended NEWS generation and publishing. Checked before "
            "any Gemini call or Telegram send. Anything other than an explicit truthy "
            "value is treated as off."
        ),
    )
    gemini_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Google AI Studio API key for the Gemini Developer API. SecretStr, like the "
            "Telegram token; never logged, never stored, never printed."
        ),
    )
    llm_model: str = Field(
        default="gemini-3.6-flash",
        description=(
            "The Gemini model id, e.g. 'models/gemini-3.6-flash'. Read from config at "
            "call time — never hard-coded as the only possible value — so a model "
            "rename or a deliberate switch needs no code change."
        ),
    )
    #: Named rather than inlined so tests and docs can reference the exact value
    #: without repeating the literal — the validator below does not read these; it
    #: looks the default up generically from the field itself, so there is only one
    #: real source of truth (the ``Field(default=...)`` calls) for it to drift from.
    #: ClassVars, so Pydantic treats them as plain class attributes, not model fields.
    DEFAULT_DAILY_POST_LIMIT: ClassVar[int] = 3
    DEFAULT_GEMINI_READ_TIMEOUT_SECONDS: ClassVar[float] = 90.0
    DEFAULT_MAX_CANDIDATE_ATTEMPTS: ClassVar[int] = 3

    daily_post_limit: int = Field(
        default=DEFAULT_DAILY_POST_LIMIT,
        ge=0,
        description=(
            "Maximum automated NEWS publications per Europe/Kyiv calendar day, counted "
            "across every workflow run that day. A scheduled run past the limit is a "
            "safe no-op, not an error."
        ),
    )
    gemini_read_timeout_seconds: float = Field(
        default=DEFAULT_GEMINI_READ_TIMEOUT_SECONDS,
        gt=0,
        description=(
            "How long to wait for Gemini's response body once the request is sent, per "
            "attempt (not the total across retries). Separate from the connect/write/pool "
            "timeouts, which are short and fixed — those guard against a network that "
            "never answers at all, not against a model that is genuinely still "
            "generating. A real GitHub Actions run needed more than the original flat "
            "30s here; see automation/gemini.py."
        ),
    )
    max_candidate_attempts: int = Field(
        default=DEFAULT_MAX_CANDIDATE_ATTEMPTS,
        ge=1,
        description=(
            "How many distinct NEWS candidates one automation run will try, in "
            "sequence, before giving up for that run — not a retry count for a single "
            "Gemini HTTP call (see gemini_read_timeout_seconds and the client's own "
            "bounded transport retries for that), and not the daily publication cap "
            "(see daily_post_limit). Exists because one candidate that fails for a "
            "reason specific to it — a 403 fetching its article, an incomplete "
            "generation, a validation rejection — should not by itself end a run that "
            "still has other eligible candidates worth trying."
        ),
    )

    @field_validator(
        "telegram_bot_token", "telegram_channel", "telegram_owner_user_id", "test_channel",
        "gemini_api_key",
        mode="before",
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

    @field_validator(
        "daily_post_limit", "gemini_read_timeout_seconds", "max_candidate_attempts",
        mode="before",
    )
    @classmethod
    def _blank_means_the_default(cls, value: object, info: ValidationInfo) -> object:
        """Same problem as ``_blank_means_unset`` above, different fix: these fields are
        not optional, so a blank string cannot become ``None`` the way it does there —
        it becomes the field's own configured default instead, looked up generically
        by field name rather than hard-coded per field, so a third field with the same
        need only has to be added to the decorator above, not given its own copy of
        this method.

        This exists specifically for GitHub Actions: ``env: AI_NEWS_DAILY_POST_LIMIT:
        ${{ vars.AI_NEWS_DAILY_POST_LIMIT }}`` always sets that key in the job's
        environment, even to ``""``, when the repository Variable has never been
        created — a plain YAML ``env:`` block has no way to omit a key conditionally.
        Without this, the very first workflow run on a repo that has not yet set that
        Variable would fail Settings validation before doing anything else.
        """
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("telegram_channel", "test_channel")
    @classmethod
    def _check_channel(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Catch a destination that Telegram will certainly reject, at startup.

        A channel configured as bare text ("my_channel") silently resolves to nothing
        useful; better to say so before a human is standing at a publish prompt. Shared
        between telegram_channel and test_channel, so the error names whichever setting
        the reader actually got wrong rather than always naming the first one.
        """
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        setting = f"AI_NEWS_{info.field_name.upper()}"
        if trimmed.startswith("@"):
            if len(trimmed) < 2:
                raise ValueError(f"{setting} is just '@'")
            return trimmed
        try:
            int(trimmed)
        except ValueError:
            raise ValueError(
                f"{setting} must be a public @username or a numeric chat id, got {trimmed!r}"
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
