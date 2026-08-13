"""Turning what a human types into an instant, or refusing to guess.

The channel is edited from Ukraine, so "10:00" means 10:00 in Kyiv — not on whatever
machine happens to run the scheduler. Everything is stored in UTC and displayed in the
channel's timezone, and the conversion happens here so it happens once.

Twice a year local time is not a function. In spring an hour does not exist; in autumn
an hour happens twice. Both are refused rather than resolved, because either resolution
would silently publish at a time the owner did not choose — and "the post went out an
hour early once a year" is exactly the kind of bug nobody finds.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

#: The channel's editorial timezone. Not the machine's.
CHANNEL_TIMEZONE = "Europe/Kyiv"

#: Convenience presets, in channel-local time. Named after parts of the day, and
#: deliberately not after "best engagement" — there is no data behind such a claim.
DAYPARTS: dict[str, tuple[int, int]] = {
    "morning": (9, 0),
    "afternoon": (13, 0),
    "evening": (19, 0),
}

_FORMATS = ("%d.%m %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M")
_RELAXED = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?\s+(\d{1,2}):(\d{2})\s*$")


class TimeError(ValueError):
    """A local time this application will not guess at."""


def zone(name: str = CHANNEL_TIMEZONE) -> ZoneInfo:
    return ZoneInfo(name)


def to_utc(local: datetime, tz_name: str = CHANNEL_TIMEZONE) -> datetime:
    """Attach the channel timezone to a naive local time and convert to UTC.

    Raises:
        TimeError: the local time does not exist, or happens twice. Both are real dates
            on the calendar and neither has one answer, so neither is guessed.
    """
    tz = zone(tz_name)
    if local.tzinfo is not None:
        raise TimeError("expected a local time without a timezone attached")

    aware = local.replace(tzinfo=tz)

    # A nonexistent time (spring forward): converting to UTC and back lands somewhere
    # else, because the wall clock skipped over it.
    round_trip = aware.astimezone(ZoneInfo("UTC")).astimezone(tz)
    if round_trip.replace(tzinfo=None) != local:
        raise TimeError(
            f"{local:%d.%m.%Y %H:%M} does not exist in {tz_name} — the clocks move "
            "forward that night. Choose another time."
        )

    # An ambiguous time (autumn back): the same wall clock happens twice, once in each
    # offset. fold=0 and fold=1 disagree about UTC, which is how the ambiguity shows.
    if aware.utcoffset() != local.replace(tzinfo=tz, fold=1).utcoffset():
        raise TimeError(
            f"{local:%d.%m.%Y %H:%M} happens twice in {tz_name} — the clocks go back "
            "that night. Choose another time."
        )

    return aware.astimezone(ZoneInfo("UTC"))


def to_local(instant: datetime, tz_name: str = CHANNEL_TIMEZONE) -> datetime:
    """A UTC instant as the owner reads it."""
    return instant.astimezone(zone(tz_name))


def parse_local(text: str, *, now: datetime, tz_name: str = CHANNEL_TIMEZONE) -> datetime:
    """Read "13.08 14:30" and return the UTC instant it means.

    A missing year is the next occurrence: typing a date that has just passed means next
    year far more often than it means the past.

    Raises:
        TimeError: unparseable, or a local time that does not exist or is ambiguous.
    """
    candidate = text.strip()
    local: datetime | None = None

    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            reference = to_local(now, tz_name)
            parsed = parsed.replace(year=reference.year)
            if parsed < reference.replace(tzinfo=None) - timedelta(days=1):
                parsed = parsed.replace(year=reference.year + 1)
        local = parsed
        break

    if local is None:
        match = _RELAXED.match(candidate)
        if match is None:
            raise TimeError(
                f"could not read {text!r} as a date and time. Try 13.08 14:30 "
                "or 2026-08-13 14:30."
            )
        day, month, year, hour, minute = match.groups()
        reference = to_local(now, tz_name)
        try:
            local = datetime(
                int(year) if year else reference.year,
                int(month), int(day), int(hour), int(minute),
            )
        except ValueError as exc:
            # The pattern matched but the numbers are not a date — "32.13 99:99", or
            # the 30th of February. Everything reaching this module is typed by a human
            # into a phone, so it has to come back as a sentence rather than a
            # traceback in a bot handler.
            raise TimeError(
                f"{text.strip()!r} is not a real date and time ({exc}). "
                "Try 13.08 14:30."
            ) from exc
        if year is None and local < reference.replace(tzinfo=None) - timedelta(days=1):
            local = local.replace(year=reference.year + 1)

    return to_utc(local, tz_name)


def daypart(
    name: str, *, now: datetime, days_ahead: int = 1, tz_name: str = CHANNEL_TIMEZONE
) -> datetime:
    """A preset like "tomorrow morning", as a UTC instant.

    Raises:
        TimeError: the preset lands on a nonexistent or ambiguous local time. Rare, and
            still refused — a convenience button must not schedule a time nobody meant.
    """
    if name not in DAYPARTS:
        raise TimeError(f"unknown daypart {name!r}; expected one of {', '.join(DAYPARTS)}")
    hour, minute = DAYPARTS[name]
    local_now = to_local(now, tz_name)
    target = (local_now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None
    )
    return to_utc(target, tz_name)


def describe(instant: datetime, tz_name: str = CHANNEL_TIMEZONE) -> str:
    """"13 Aug 2026 · 10:00 Europe/Kyiv" — what a confirmation screen shows."""
    local = to_local(instant, tz_name)
    return f"{local:%d %b %Y · %H:%M} {tz_name}"
