"""Test-channel publication history — durability for ``--test`` dedup only.

``mode=test`` intentionally never touches the canonical database (see
:func:`automation.pipeline.isolated_connection`): every write during a test run lands
in an ephemeral in-memory copy that is discarded the moment the run ends, specifically
so a test send can never affect production eligibility, the daily limit, or dedup. That
stays true here too — this module never opens the canonical database and has no idea
what it contains.

But that isolation has a side effect: nothing stops two independent test runs from
picking the same article, since each one starts from an identical canonical snapshot
with no memory of what an earlier test run already sent (this is exactly what produced
two copies of the same post in the test channel — see the run history around 2026-08-18).
This module is the fix: a small, bounded, append-only record of which source URLs have
already been sent to the test channel, stored in its own file, read and written by
nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_news_editor.domain.clock import now_utc

#: Retention is a count, not a time window — simpler and deterministic, and this
#: history carries no production meaning worth keeping any longer than that.
MAX_ENTRIES = 50

DEFAULT_FILENAME = "test_publish_history.json"


@dataclass(frozen=True, slots=True)
class TestPublication:
    source_url: str
    published_at: str
    telegram_message_id: int | None


def load(path: Path) -> list[TestPublication]:
    """Every recorded test publication, oldest first.

    A missing, empty or unreadable file is treated as "no history yet" rather than an
    error — this file existing at all is an optimization, not a requirement anything
    else depends on.
    """
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []

    result: list[TestPublication] = []
    for entry in entries:
        if not isinstance(entry, dict) or "source_url" not in entry or "published_at" not in entry:
            continue
        result.append(
            TestPublication(
                source_url=str(entry["source_url"]),
                published_at=str(entry["published_at"]),
                telegram_message_id=entry.get("telegram_message_id"),
            )
        )
    return result


def already_test_published(path: Path, source_url: str) -> bool:
    """Whether this exact URL has already been sent to the test channel, per this file."""
    return any(entry.source_url == source_url for entry in load(path))


def record(path: Path, *, source_url: str, message_id: int | None) -> None:
    """Append one entry, keeping only the most recent :data:`MAX_ENTRIES`.

    Called only after a real, successful test-channel send — never speculatively, and
    never for a dry run (which has no message_id to record in the first place).
    """
    entries = load(path)
    entries.append(
        TestPublication(
            source_url=source_url,
            published_at=now_utc().isoformat(),
            telegram_message_id=message_id,
        )
    )
    entries = entries[-MAX_ENTRIES:]

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "entries": [
            {
                "source_url": entry.source_url,
                "published_at": entry.published_at,
                "telegram_message_id": entry.telegram_message_id,
            }
            for entry in entries
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
