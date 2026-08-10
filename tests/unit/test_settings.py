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
    def test_no_credential_settings_exist_yet(self) -> None:
        """Phase 1 consumes no secrets; fake requirements would be misleading."""
        forbidden = {"api_key", "token", "secret", "password"}
        fields = set(Settings.model_fields)
        assert not any(any(word in field for word in forbidden) for field in fields)
