"""Secret redaction for log output.

Phase 1 consumes no secrets, but the filter is installed from the start: a redaction
layer added after the first credential exists is a redaction layer added after the
first leak. Patterns cover the credential shapes this project will actually use.
"""

from __future__ import annotations

import logging
import re
from typing import Any

MASK = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # A token inside a Bot API URL: .../bot<id>:<secret>/sendMessage. This has to come
    # first and match on its own, because the standalone token pattern below needs a
    # word boundary before the digits and "bot123456" does not provide one — an httpx
    # exception carrying the request URL would otherwise print the token in full.
    re.compile(r"(?i)/bot\d{6,12}:[A-Za-z0-9_-]{20,}"),
    # Telegram bot token: <numeric bot id>:<35-char secret>
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    # OpenAI-style keys and common vendor prefixes
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    # Authorization headers
    re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9._~+/=-]{12,}"),
    # key=value / key: value forms for anything that names itself a secret
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_?KEY)[A-Z0-9_]*)\s*[=:]\s*"
        r"['\"]?([^\s'\"]{6,})"
    ),
)


def redact(text: str) -> str:
    """Replace anything matching a known credential shape with :data:`MASK`."""
    for pattern in _PATTERNS:
        # Two-group patterns match "NAME = value"; keep the name, mask only the value.
        replacement = rf"\1={MASK}" if pattern.groups == 2 else MASK
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    # Not a string, but it will become one when the record is formatted. httpx logs the
    # request URL as an httpx.URL object, and that URL contains the bot token — checking
    # only str arguments let the token through into the log. The original object is kept
    # whenever redaction changed nothing, so ordinary values keep their type and repr.
    try:
        text = str(value)
    except Exception:  # pragma: no cover - a __str__ that raises is not our problem
        return value
    cleaned = redact(text)
    return cleaned if cleaned != text else value


class RedactionFilter(logging.Filter):
    """Scrub credentials from the message, its arguments, and any extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = _redact_value(record.args)
        for key, value in record.__dict__.items():
            if isinstance(value, str) and key not in {"msg", "name", "levelname", "pathname"}:
                record.__dict__[key] = redact(value)
        return True
