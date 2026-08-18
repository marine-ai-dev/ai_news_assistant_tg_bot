"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_news_editor.domain.errors import ConfigurationError
from ai_news_editor.settings import Settings, get_settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestDefaults:
    def test_database_defaults_under_data_dir(self, tmp_path: Path) -> None:
        settings = _settings(data_dir=tmp_path)
        assert settings.resolved_database_path == (tmp_path / "ai_news.sqlite3").resolve()

    def test_explicit_database_path_wins(self, tmp_path: Path) -> None:
        target = tmp_path / "custom" / "news.sqlite3"
        settings = _settings(data_dir=tmp_path, database_path=target)
        assert settings.resolved_database_path == target.resolve()

    def test_resolved_path_is_absolute(self) -> None:
        assert _settings(data_dir=Path("data")).resolved_database_path.is_absolute()

    def test_auto_publish_is_off_by_default(self) -> None:
        assert _settings().auto_publish_enabled is False


class TestValidation:
    def test_enabling_auto_publish_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="explicit human approval"):
            _settings(auto_publish_enabled=True)

    def test_enabling_auto_publish_via_environment_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_AUTO_PUBLISH_ENABLED", "true")
        get_settings.cache_clear()
        with pytest.raises(ConfigurationError, match="explicit human approval"):
            get_settings()
        get_settings.cache_clear()

    def test_invalid_log_level_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _settings(log_level="CHATTY")

    def test_invalid_log_format_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _settings(log_format="xml")

    def test_environment_variables_are_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_NEWS_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("AI_NEWS_ENVIRONMENT", "ci")
        settings = _settings()
        assert settings.log_level == "DEBUG"
        assert settings.environment == "ci"


class TestDataDir:
    def test_ensure_data_dir_creates_nested_directories(self, tmp_path: Path) -> None:
        settings = _settings(database_path=tmp_path / "a" / "b" / "news.sqlite3")
        created = settings.ensure_data_dir()
        assert created.is_dir()

    def test_ensure_data_dir_is_idempotent(self, tmp_path: Path) -> None:
        settings = _settings(data_dir=tmp_path)
        assert settings.ensure_data_dir() == settings.ensure_data_dir()


class TestSecrets:
    def test_only_two_credentials_exist(self) -> None:
        """The Telegram token, and — since the GitHub Actions automation pipeline —
        the Gemini API key. Nothing else in this application needs a credential.

        This used to assert exactly one. It now asserts exactly two, deliberately: the
        automated NEWS pipeline calls the Gemini Developer API, which needs its own
        key, distinct from the Telegram token and never sent to Telegram or logged
        alongside it. See test_no_claude_or_openai_credential_exists for the invariant
        that still holds unconditionally — no OTHER model provider ever gets one.
        """
        forbidden = {"api_key", "token", "secret", "password"}
        credential_fields = {
            field
            for field in Settings.model_fields
            if any(word in field for word in forbidden)
        }
        assert credential_fields == {"telegram_bot_token", "gemini_api_key"}

    def test_the_token_is_a_secret_string(self) -> None:
        """SecretStr, so printing settings or a traceback cannot leak it."""
        settings = Settings(telegram_bot_token="123456:ABCDEF")  # type: ignore[arg-type]
        assert "123456:ABCDEF" not in repr(settings)
        assert "123456:ABCDEF" not in str(settings.telegram_bot_token)
        assert settings.telegram_bot_token is not None
        assert settings.telegram_bot_token.get_secret_value() == "123456:ABCDEF"


class TestDailyPostLimit:
    """GitHub Actions ``env:`` blocks always set the key, even to "", when the
    referenced repository Variable does not exist — a plain YAML env block has no way
    to omit a key conditionally. This is what makes the very first automation workflow
    run, on a repo that has not yet created ``AI_NEWS_DAILY_POST_LIMIT``, survive."""

    def test_blank_falls_back_to_the_default_not_a_parse_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_DAILY_POST_LIMIT", "")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.daily_post_limit == Settings.DEFAULT_DAILY_POST_LIMIT == 3

    def test_unset_is_the_same_default(self) -> None:
        assert _settings().daily_post_limit == 3

    def test_an_explicit_value_from_the_environment_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_DAILY_POST_LIMIT", "7")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.daily_post_limit == 7

    def test_a_negative_value_is_still_refused(self) -> None:
        with pytest.raises(ValidationError):
            _settings(daily_post_limit=-1)


class TestGeminiReadTimeout:
    """Same blank-string-from-GitHub-Actions problem as daily_post_limit above, and
    the same fix — see Settings._blank_means_the_default, which now covers both."""

    def test_default_is_90_seconds(self) -> None:
        assert Settings.DEFAULT_GEMINI_READ_TIMEOUT_SECONDS == 90.0
        assert _settings().gemini_read_timeout_seconds == 90.0

    def test_blank_falls_back_to_the_default_not_a_parse_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_GEMINI_READ_TIMEOUT_SECONDS", "")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.gemini_read_timeout_seconds == 90.0

    def test_an_explicit_value_from_the_environment_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_GEMINI_READ_TIMEOUT_SECONDS", "45")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.gemini_read_timeout_seconds == 45.0

    def test_zero_or_negative_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _settings(gemini_read_timeout_seconds=0)
        with pytest.raises(ValidationError):
            _settings(gemini_read_timeout_seconds=-5)


class TestMaxCandidateAttempts:
    """Same blank-string-from-GitHub-Actions problem as daily_post_limit and
    gemini_read_timeout_seconds above, and the same fix."""

    def test_default_is_3(self) -> None:
        assert Settings.DEFAULT_MAX_CANDIDATE_ATTEMPTS == 3
        assert _settings().max_candidate_attempts == 3

    def test_blank_falls_back_to_the_default_not_a_parse_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_MAX_CANDIDATE_ATTEMPTS", "")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.max_candidate_attempts == 3

    def test_an_explicit_value_from_the_environment_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_MAX_CANDIDATE_ATTEMPTS", "5")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.max_candidate_attempts == 5

    def test_zero_or_negative_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _settings(max_candidate_attempts=0)
        with pytest.raises(ValidationError):
            _settings(max_candidate_attempts=-1)


class TestAFreshCloneStarts:
    """The .env.example a newcomer copies must actually work.

    This is here because a clean-room clone caught it: `.env.example` ships every
    Telegram variable with an empty value, and an empty string is not an integer, so
    the first command after the documented quick-start died on a validation error
    about a setting the reader had never touched.
    """

    @pytest.mark.parametrize(
        "variable",
        [
            "AI_NEWS_TELEGRAM_BOT_TOKEN",
            "AI_NEWS_TELEGRAM_CHANNEL",
            "AI_NEWS_TELEGRAM_OWNER_USER_ID",
            "AI_NEWS_GEMINI_API_KEY",
        ],
    )
    def test_a_blank_value_means_not_configured(
        self, variable: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(variable, "")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert getattr(settings, variable.removeprefix("AI_NEWS_").lower()) is None

    def test_whitespace_is_not_a_configured_value_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_NEWS_TELEGRAM_OWNER_USER_ID", "   ")
        assert Settings(_env_file=None).telegram_owner_user_id is None  # type: ignore[call-arg]

    def test_the_shipped_example_env_loads(self, tmp_path: Path) -> None:
        """Parse the real .env.example, exactly as 'cp .env.example .env' produces it."""
        example = Path(__file__).resolve().parents[2] / ".env.example"
        target = tmp_path / ".env"
        target.write_text(example.read_text(), encoding="utf-8")

        settings = Settings(_env_file=target)  # type: ignore[call-arg]

        # It loads, and the three Telegram values are correctly seen as unset rather
        # than as empty strings pretending to be configuration.
        assert settings.telegram_bot_token is None
        assert settings.telegram_channel is None
        assert settings.telegram_owner_user_id is None
        assert settings.gemini_api_key is None
        # And the non-secret defaults did come through, so the file is really parsed.
        assert settings.auto_publish_enabled is False
        assert settings.automation_enabled is False
        assert settings.channel_handle.startswith("@")

    def test_the_example_contains_no_real_secret(self) -> None:
        """Every secret in the template is a blank placeholder, not somebody's value."""
        example = Path(__file__).resolve().parents[2] / ".env.example"
        for line in example.read_text(encoding="utf-8").splitlines():
            if line.startswith(
                ("AI_NEWS_TELEGRAM_BOT_TOKEN", "AI_NEWS_TELEGRAM_OWNER", "AI_NEWS_GEMINI_API_KEY")
            ):
                assert line.split("=", 1)[1].strip() == "", f"{line.split('=')[0]} has a value"
