"""Conversions between domain values and their SQLite representations."""

from __future__ import annotations

# SQLite's INTEGER is a signed 64-bit value, while a simhash is an unsigned 64-bit
# value, so any hash with the top bit set overflows on insert. These two functions
# reinterpret the same bits across that boundary. They are exact inverses, and Hamming
# distance is unaffected because no bits move — only their signed interpretation does.
_UINT64 = 1 << 64
_INT64_MAX = 1 << 63


def simhash_to_storage(value: int | None) -> int | None:
    """Reinterpret an unsigned 64-bit simhash as signed, for storage."""
    if value is None:
        return None
    return value - _UINT64 if value >= _INT64_MAX else value


def simhash_from_storage(value: int | None) -> int | None:
    """Reinterpret a stored signed 64-bit value as an unsigned simhash."""
    if value is None:
        return None
    return value + _UINT64 if value < 0 else value
