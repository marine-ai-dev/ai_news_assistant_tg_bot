"""UTC time handling.

The application is internally UTC-only. Naive datetimes are rejected at the model
boundary rather than silently assumed to be local time, because a wrong publication
timestamp is the kind of bug that is invisible until it matters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator


def now_utc() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC; reject naive datetimes."""
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed; provide a timezone-aware value")
    return value.astimezone(UTC)


def _coerce(value: object) -> object:
    """Parse ISO-8601 strings so values read back from SQLite validate cleanly."""
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def to_iso(value: datetime) -> str:
    """Serialize to the storage representation: ISO-8601, always UTC, always offset-aware."""
    return ensure_utc(value).isoformat()


def from_iso(value: str) -> datetime:
    """Parse the storage representation back into an aware UTC datetime."""
    return ensure_utc(datetime.fromisoformat(value))


#: Datetime field type used across all domain models.
UtcDatetime = Annotated[datetime, BeforeValidator(_coerce), AfterValidator(ensure_utc)]
