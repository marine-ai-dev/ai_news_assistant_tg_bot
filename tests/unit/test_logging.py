"""Structured logging and secret redaction."""

from __future__ import annotations

import json
import logging

import pytest

from ai_news_editor.observability.logging import (
    ConsoleFormatter,
    JsonFormatter,
    RunIdFilter,
    configure_logging,
    current_run_id,
    new_run_id,
)
from ai_news_editor.observability.redaction import MASK, RedactionFilter, redact


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", logging.INFO, __file__, 1, message, None, None)
    record.__dict__.update(extra)
    return record


class TestJsonFormatter:
    def test_emits_one_json_object(self) -> None:
        payload = json.loads(JsonFormatter().format(_record("hello")))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.logger"

    def test_timestamp_is_utc(self) -> None:
        payload = json.loads(JsonFormatter().format(_record("hello")))
        assert payload["ts"].endswith("+00:00")

    def test_extra_fields_are_included(self) -> None:
        payload = json.loads(JsonFormatter().format(_record("hello", version=7)))
        assert payload["version"] == 7

    def test_exception_is_serialisable(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = _record("failed")
            record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in payload["exception"]


class TestConsoleFormatter:
    def test_includes_logger_and_message(self) -> None:
        line = ConsoleFormatter().format(_record("hello"))
        assert "test.logger" in line
        assert "hello" in line
        assert "INFO" in line

    def test_renders_extra_fields(self) -> None:
        assert "count=3" in ConsoleFormatter().format(_record("hello", count=3))


class TestRunId:
    def test_new_run_id_is_recorded(self) -> None:
        run_id = new_run_id()
        assert current_run_id() == run_id

    def test_filter_attaches_run_id(self) -> None:
        run_id = new_run_id()
        record = _record("hello")
        RunIdFilter().filter(record)
        assert record.run_id == run_id  # type: ignore[attr-defined]

    def test_run_ids_differ(self) -> None:
        assert new_run_id() != new_run_id()


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "token is 123456789:" + "A" * 35,
            "key sk-" + "z" * 32,
            "key sk-ant-" + "z" * 30,
            "Authorization: Bearer " + "y" * 22,
            "TELEGRAM_BOT_TOKEN=123456789:" + "A" * 35,
            "api_key: supersecretvalue123",
        ],
    )
    def test_credential_shapes_are_masked(self, text: str) -> None:
        result = redact(text)
        assert MASK in result
        for secret in ("" + "A" * 35, "supersecretvalue123"):
            assert secret not in result

    def test_ordinary_text_is_untouched(self) -> None:
        text = "collected 42 items from openai_news in 1.2s"
        assert redact(text) == text

    def test_filter_scrubs_message_and_extras(self) -> None:
        record = _record(
            "connecting with sk-" + "z" * 32,
            detail="sk-" + "z" * 32,
        )
        RedactionFilter().filter(record)
        assert MASK in record.msg
        assert MASK in record.detail  # type: ignore[attr-defined]

    def test_nested_structures_are_scrubbed(self) -> None:
        """Credentials hidden inside a dict or tuple argument must not survive."""
        record = logging.LogRecord(
            "t",
            logging.INFO,
            __file__,
            1,
            "context %s",
            ({"key": "sk-" + "z" * 28, "count": 3},),
            None,
        )
        RedactionFilter().filter(record)
        message = record.getMessage()
        assert "sk-" + "z" * 28 not in message
        assert "count" in message

    def test_filter_scrubs_format_arguments(self) -> None:
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "using %s", ("sk-" + "z" * 28,), None
        )
        RedactionFilter().filter(record)
        assert MASK in record.getMessage()


class TestConfigureLogging:
    def test_installs_a_single_handler(self) -> None:
        configure_logging(level="DEBUG", fmt="json")
        configure_logging(level="INFO", fmt="console")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.level == logging.INFO

    def test_handler_carries_both_filters(self) -> None:
        configure_logging()
        filters = logging.getLogger().handlers[0].filters
        assert any(isinstance(f, RunIdFilter) for f in filters)
        assert any(isinstance(f, RedactionFilter) for f in filters)
