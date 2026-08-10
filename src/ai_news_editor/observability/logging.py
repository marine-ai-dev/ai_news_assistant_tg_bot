"""Structured logging on the standard library only.

Two formats: ``json`` for machine consumption and ``console`` for humans. Both stamp
UTC timestamps, the logger name, and a ``run_id`` that ties every line of one pipeline
run together — the correlation id that will eventually connect a published post to the
raw bytes it came from.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ai_news_editor.observability.redaction import RedactionFilter

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)

#: Attributes ``logging`` puts on every record; anything else is caller-supplied extra.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
    "run_id",
}


def new_run_id() -> str:
    """Start a new correlation scope and return its id."""
    run_id = uuid4().hex[:12]
    _run_id.set(run_id)
    return run_id


def current_run_id() -> str | None:
    """Return the correlation id of the active run, if any."""
    return _run_id.get()


class RunIdFilter(logging.Filter):
    """Attach the active run id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get() or "-"
        return True


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Compact single-line output for interactive use."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = getattr(record, "run_id", "-")
        line = f"{ts} {record.levelname:<7} [{run_id}] {record.name}: {record.getMessage()}"
        extras = _extras(record)
        if extras:
            line += " " + " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else ConsoleFormatter()

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(formatter)
    handler.addFilter(RunIdFilter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger."""
    return logging.getLogger(name)
