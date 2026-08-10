"""Persistence for per-source conditional-fetch state."""

from __future__ import annotations

import sqlite3

from ai_news_editor.domain.clock import now_utc, to_iso
from ai_news_editor.domain.enums import FetchOutcome
from ai_news_editor.domain.models import SourceFetchState


def _to_domain(row: sqlite3.Row) -> SourceFetchState:
    return SourceFetchState.model_validate(dict(row))


class SourceFetchStateRepository:
    """Reads and writes ``source_fetch_state``.

    Rows are created lazily: a source that has never been fetched simply has no state,
    which :meth:`get` reports as an empty state rather than an error.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def get(self, source_id: str) -> SourceFetchState:
        """Current state, or a blank one for a source fetched for the first time."""
        row = self._conn.execute(
            "SELECT * FROM source_fetch_state WHERE source_id = ?", (source_id,)
        ).fetchone()
        return _to_domain(row) if row else SourceFetchState(source_id=source_id)

    def find(self, source_id: str) -> SourceFetchState | None:
        row = self._conn.execute(
            "SELECT * FROM source_fetch_state WHERE source_id = ?", (source_id,)
        ).fetchone()
        return _to_domain(row) if row else None

    def record_success(
        self,
        source_id: str,
        *,
        outcome: FetchOutcome,
        etag: str | None,
        last_modified: str | None,
        http_status: int | None,
    ) -> SourceFetchState:
        """Store fresh validators and reset the failure counter.

        Applies to ``OK`` and ``NOT_MODIFIED`` alike: a 304 is a successful check.
        Validators are only overwritten when the server supplied new ones, so a 304
        that echoes nothing does not erase the ETag that produced it.
        """
        now = now_utc()
        state = SourceFetchState(
            source_id=source_id,
            etag=etag,
            last_modified=last_modified,
            last_attempt_at=now,
            last_success_at=now,
            last_outcome=outcome,
            last_http_status=http_status,
            last_error=None,
            consecutive_failures=0,
            updated_at=now,
        )
        self._conn.execute(
            """
            INSERT INTO source_fetch_state (source_id, etag, last_modified, last_attempt_at,
                                            last_success_at, last_outcome, last_http_status,
                                            last_error, consecutive_failures, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
            ON CONFLICT (source_id) DO UPDATE SET
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at,
                last_outcome = excluded.last_outcome,
                last_http_status = excluded.last_http_status,
                last_error = NULL,
                consecutive_failures = 0,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                etag,
                last_modified,
                to_iso(now),
                to_iso(now),
                outcome.value,
                http_status,
                to_iso(now),
            ),
        )
        return state

    def record_failure(
        self, source_id: str, *, error: str, http_status: int | None = None
    ) -> SourceFetchState:
        """Record a failed attempt, incrementing the consecutive-failure counter.

        Existing validators and ``last_success_at`` are preserved: a transient outage
        should not force a full re-download once the source recovers.
        """
        now = now_utc()
        self._conn.execute(
            """
            INSERT INTO source_fetch_state (source_id, last_attempt_at, last_outcome,
                                            last_http_status, last_error,
                                            consecutive_failures, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT (source_id) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                last_outcome = excluded.last_outcome,
                last_http_status = excluded.last_http_status,
                last_error = excluded.last_error,
                consecutive_failures = source_fetch_state.consecutive_failures + 1,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                to_iso(now),
                FetchOutcome.ERROR.value,
                http_status,
                error[:2000],
                to_iso(now),
            ),
        )
        return self.get(source_id)

    def list_all(self) -> list[SourceFetchState]:
        rows = self._conn.execute(
            "SELECT * FROM source_fetch_state ORDER BY source_id"
        ).fetchall()
        return [_to_domain(row) for row in rows]
