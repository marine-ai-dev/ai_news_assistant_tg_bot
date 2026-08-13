"""Persistence for scheduling intent, and the claim that keeps two workers apart.

Most of this is ordinary reads and writes. One method is not: :meth:`claim`. Two
scheduler processes on one Mac is not a hypothetical — a forgotten terminal window, a
restart, a launchd job that was never removed — and if both decide the same item is due,
readers see the post twice.

The defence is a single conditional UPDATE. SQLite serialises writers, so the losing
worker's UPDATE matches zero rows and it simply moves on. Nothing is held in Python
memory, so the guarantee survives a process that never learns it lost.

Every change also appends an event. The queue row says where an item is; the events say
how it got there, which is what a person needs months later when asking why a post went
out at eleven instead of ten.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from ai_news_editor.domain.clock import from_iso, now_utc, to_iso
from ai_news_editor.domain.enums import QueueStatus
from ai_news_editor.domain.errors import EntityNotFoundError
from ai_news_editor.domain.models import QueueItem
from ai_news_editor.observability.redaction import redact

#: How long a worker's claim on an item is good for. Long enough that a slow upload with
#: a large PDF finishes comfortably; short enough that a killed process does not lock a
#: post out of the channel for an afternoon.
DEFAULT_LEASE = timedelta(minutes=10)


def _to_domain(row: sqlite3.Row) -> QueueItem:
    return QueueItem.model_validate(dict(row))


class PublicationQueueRepository:
    """Reads and writes ``publication_queue``, and appends to its event log."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    # ------------------------------------------------------------------ writing

    def add(self, item: QueueItem, *, actor: str) -> QueueItem:
        """Record a scheduling decision.

        Raises:
            sqlite3.IntegrityError: this version already has a live schedule. The unique
                partial index is what makes that a database fact rather than a race.
        """
        self._conn.execute(
            """
            INSERT INTO publication_queue (id, draft_id, draft_version_id,
                                           review_decision_id, content_hash, channel,
                                           scheduled_for, display_timezone, status,
                                           hold_reason, claimed_by, claimed_at,
                                           lease_expires_at, publication_id, queued_at,
                                           last_checked_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.id),
                str(item.draft_id),
                str(item.draft_version_id),
                str(item.review_decision_id),
                item.content_hash,
                item.channel,
                to_iso(item.scheduled_for),
                item.display_timezone,
                item.status.value,
                item.hold_reason,
                item.claimed_by,
                to_iso(item.claimed_at) if item.claimed_at else None,
                to_iso(item.lease_expires_at) if item.lease_expires_at else None,
                str(item.publication_id) if item.publication_id else None,
                to_iso(item.queued_at),
                to_iso(item.last_checked_at) if item.last_checked_at else None,
                to_iso(item.updated_at),
            ),
        )
        self.log(
            item.id,
            event="QUEUED",
            to_status=item.status,
            scheduled_for=item.scheduled_for,
            actor=actor,
        )
        return item

    def set_status(
        self,
        queue_id: UUID,
        status: QueueStatus,
        *,
        actor: str,
        reason: str | None = None,
        event: str | None = None,
        publication_id: UUID | None = None,
        release_claim: bool = True,
        now: datetime | None = None,
    ) -> QueueItem:
        """Move an item, recording why.

        The claim is dropped by default: every status this moves to is one where no
        worker should still be holding the item.
        """
        current = self.get(queue_id)
        moment = now or now_utc()
        # Redacted on the way in. A hold reason can quote an upstream error, and the
        # database is one of the places a token must never reach.
        clean = redact(reason) if reason else None
        # The claim columns are cleared by the same statement rather than a second one,
        # so an item can never be seen as held by a worker that has already finished.
        self._conn.execute(
            """
            UPDATE publication_queue
               SET status = ?, hold_reason = ?, updated_at = ?, last_checked_at = ?,
                   publication_id = COALESCE(?, publication_id),
                   claimed_by = CASE WHEN ? THEN NULL ELSE claimed_by END,
                   claimed_at = CASE WHEN ? THEN NULL ELSE claimed_at END,
                   lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END
             WHERE id = ?
            """,
            (
                status.value,
                clean,
                to_iso(moment),
                to_iso(moment),
                str(publication_id) if publication_id else None,
                release_claim,
                release_claim,
                release_claim,
                str(queue_id),
            ),
        )
        self.log(
            queue_id,
            event=event or status.value,
            from_status=current.status,
            to_status=status,
            detail=clean,
            actor=actor,
        )
        return self.get(queue_id)

    def reschedule(
        self, queue_id: UUID, when: datetime, *, actor: str, timezone_name: str | None = None
    ) -> QueueItem:
        """Move a waiting item to a new time.

        Only a SCHEDULED item can move. One already claimed, published or held is not
        rescheduled behind the worker's back; the owner resolves it first.

        Raises:
            ValueError: the item is not waiting.
        """
        current = self.get(queue_id)
        if current.status is not QueueStatus.SCHEDULED:
            raise ValueError(
                f"queue item {queue_id} is {current.status.value}, and only a SCHEDULED "
                "item can be moved to another time"
            )
        moment = now_utc()
        self._conn.execute(
            """
            UPDATE publication_queue
               SET scheduled_for = ?, display_timezone = COALESCE(?, display_timezone),
                   updated_at = ?
             WHERE id = ? AND status = 'SCHEDULED'
            """,
            (to_iso(when), timezone_name, to_iso(moment), str(queue_id)),
        )
        self.log(
            queue_id,
            event="RESCHEDULED",
            from_status=QueueStatus.SCHEDULED,
            to_status=QueueStatus.SCHEDULED,
            scheduled_for=when,
            detail=f"was {to_iso(current.scheduled_for)}",
            actor=actor,
        )
        return self.get(queue_id)

    # ------------------------------------------------------------------ claiming

    def claim(
        self,
        queue_id: UUID,
        *,
        worker: str,
        now: datetime | None = None,
        lease: timedelta = DEFAULT_LEASE,
    ) -> QueueItem | None:
        """Take exclusive ownership of a due item, or return None if someone else has it.

        One conditional UPDATE, and the condition is the whole safety property. Two
        workers running this concurrently both attempt it; SQLite serialises them; the
        second matches no rows because the status is no longer SCHEDULED. There is no
        window between deciding and claiming, because they are the same statement.

        An item whose lease has expired can be reclaimed — a crashed worker must not lock
        a post out forever. That is safe because reclaiming grants no permission to
        send: the caller re-runs every precondition, and a draft the dead worker had
        already moved to PUBLISHING no longer has a valid authorization, so it is held
        for a human rather than published a second time.
        """
        moment = now or now_utc()
        expiry = moment + lease
        cursor = self._conn.execute(
            """
            UPDATE publication_queue
               SET status = 'PROCESSING', claimed_by = ?, claimed_at = ?,
                   lease_expires_at = ?, updated_at = ?, last_checked_at = ?
             WHERE id = ?
               AND scheduled_for <= ?
               AND (
                     status = 'SCHEDULED'
                     OR (status = 'PROCESSING' AND lease_expires_at IS NOT NULL
                         AND lease_expires_at <= ?)
                   )
            """,
            (
                worker,
                to_iso(moment),
                to_iso(expiry),
                to_iso(moment),
                to_iso(moment),
                str(queue_id),
                to_iso(moment),
                to_iso(moment),
            ),
        )
        if cursor.rowcount != 1:
            return None
        self.log(queue_id, event="CLAIMED", to_status=QueueStatus.PROCESSING, actor=worker)
        return self.get(queue_id)

    def release(self, queue_id: UUID, *, worker: str, reason: str = "released") -> QueueItem:
        """Put a claimed item back without publishing it."""
        moment = now_utc()
        self._conn.execute(
            """
            UPDATE publication_queue
               SET status = 'SCHEDULED', claimed_by = NULL, claimed_at = NULL,
                   lease_expires_at = NULL, updated_at = ?, last_checked_at = ?
             WHERE id = ? AND status = 'PROCESSING'
            """,
            (to_iso(moment), to_iso(moment), str(queue_id)),
        )
        self.log(
            queue_id,
            event="RELEASED",
            from_status=QueueStatus.PROCESSING,
            to_status=QueueStatus.SCHEDULED,
            detail=reason,
            actor=worker,
        )
        return self.get(queue_id)

    def stale_claims(self, *, now: datetime | None = None) -> list[QueueItem]:
        """Items a worker took and never finished."""
        moment = now or now_utc()
        rows = self._conn.execute(
            """
            SELECT * FROM publication_queue
             WHERE status = 'PROCESSING' AND lease_expires_at IS NOT NULL
               AND lease_expires_at <= ?
             ORDER BY scheduled_for
            """,
            (to_iso(moment),),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    # ------------------------------------------------------------------ reading

    def get(self, queue_id: UUID) -> QueueItem:
        row = self._conn.execute(
            "SELECT * FROM publication_queue WHERE id = ?", (str(queue_id),)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"queue item {queue_id} not found")
        return _to_domain(row)

    def find(self, prefix: str) -> QueueItem | None:
        """Resolve an id, or an unambiguous prefix of one — queue ids get typed by hand."""
        rows = self._conn.execute(
            "SELECT * FROM publication_queue WHERE id LIKE ? || '%' LIMIT 2", (prefix.lower(),)
        ).fetchall()
        return _to_domain(rows[0]) if len(rows) == 1 else None

    def due(self, *, now: datetime | None = None, limit: int = 50) -> list[QueueItem]:
        """Waiting items whose time has come, earliest first."""
        moment = now or now_utc()
        rows = self._conn.execute(
            """
            SELECT * FROM publication_queue
             WHERE status = 'SCHEDULED' AND scheduled_for <= ?
             ORDER BY scheduled_for
             LIMIT ?
            """,
            (to_iso(moment), limit),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def next_scheduled(self, *, now: datetime | None = None) -> QueueItem | None:
        """The soonest item still in the future, so a loop knows how long it may sleep."""
        moment = now or now_utc()
        row = self._conn.execute(
            """
            SELECT * FROM publication_queue
             WHERE status = 'SCHEDULED' AND scheduled_for > ?
             ORDER BY scheduled_for LIMIT 1
            """,
            (to_iso(moment),),
        ).fetchone()
        return _to_domain(row) if row else None

    def active_for_version(self, draft_version_id: UUID, channel: str) -> QueueItem | None:
        """A live schedule for this exact version, if there is one."""
        row = self._conn.execute(
            """
            SELECT * FROM publication_queue
             WHERE draft_version_id = ? AND channel = ?
               AND status IN ('SCHEDULED', 'PROCESSING')
             LIMIT 1
            """,
            (str(draft_version_id), channel),
        ).fetchone()
        return _to_domain(row) if row else None

    def active_for_draft(self, draft_id: UUID) -> list[QueueItem]:
        """Live schedules for any version of a draft — including versions now superseded."""
        rows = self._conn.execute(
            """
            SELECT * FROM publication_queue
             WHERE draft_id = ? AND status IN ('SCHEDULED', 'PROCESSING')
             ORDER BY scheduled_for
            """,
            (str(draft_id),),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def list_upcoming(self, *, limit: int = 50) -> list[QueueItem]:
        rows = self._conn.execute(
            """
            SELECT * FROM publication_queue
             WHERE status IN ('SCHEDULED', 'PROCESSING')
             ORDER BY scheduled_for LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def list_all(self, *, limit: int = 100) -> list[QueueItem]:
        rows = self._conn.execute(
            "SELECT * FROM publication_queue ORDER BY scheduled_for DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def list_needing_attention(self, *, limit: int = 50) -> list[QueueItem]:
        rows = self._conn.execute(
            """
            SELECT * FROM publication_queue
             WHERE status IN ('STALE_REVIEW_REQUIRED', 'HOLD_FOR_REVIEW', 'FAILED',
                              'UNCERTAIN')
             ORDER BY scheduled_for DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def neighbours(
        self, when: datetime, *, within: timedelta, exclude: UUID | None = None
    ) -> list[QueueItem]:
        """Other live items scheduled close to a given time.

        Used to warn rather than to refuse. Two posts twenty minutes apart may be
        deliberate; the owner should just be told before it happens rather than after.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM publication_queue
             WHERE status IN ('SCHEDULED', 'PROCESSING')
               AND scheduled_for BETWEEN ? AND ?
               AND (? IS NULL OR id <> ?)
             ORDER BY scheduled_for
            """,
            (
                to_iso(when - within),
                to_iso(when + within),
                str(exclude) if exclude else None,
                str(exclude) if exclude else "",
            ),
        ).fetchall()
        return [_to_domain(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM publication_queue GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    # ------------------------------------------------------------------ events

    def log(
        self,
        queue_id: UUID,
        *,
        event: str,
        actor: str,
        from_status: QueueStatus | None = None,
        to_status: QueueStatus | None = None,
        scheduled_for: datetime | None = None,
        detail: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO publication_queue_events (id, queue_id, event, from_status,
                                                  to_status, scheduled_for, detail,
                                                  actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                str(queue_id),
                event,
                from_status.value if from_status else None,
                to_status.value if to_status else None,
                to_iso(scheduled_for) if scheduled_for else None,
                redact(detail) if detail else None,
                actor,
                to_iso(now_utc()),
            ),
        )

    def history(self, queue_id: UUID) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM publication_queue_events WHERE queue_id = ? ORDER BY created_at",
            (str(queue_id),),
        ).fetchall()

    def first_scheduled_for(self, queue_id: UUID) -> datetime | None:
        """The time an item was originally queued for, before any rescheduling."""
        row = self._conn.execute(
            """
            SELECT scheduled_for FROM publication_queue_events
             WHERE queue_id = ? AND event = 'QUEUED' AND scheduled_for IS NOT NULL
             ORDER BY created_at LIMIT 1
            """,
            (str(queue_id),),
        ).fetchone()
        return from_iso(row["scheduled_for"]) if row else None


def invalidate_active(
    conn: sqlite3.Connection, draft_id: UUID, *, reason: str, actor: str
) -> int:
    """Stop every waiting schedule for a draft, in the caller's transaction.

    Lives here as a plain function, and is called from ``DraftRepository`` inside the
    same transaction that appends a version or changes a status. That placement is the
    point: the guarantee must not depend on remembering to call a service afterwards.
    Editing a draft and invalidating its schedule are one atomic act, so there is never
    an instant where a superseded version is still scheduled to publish.

    Items being published right now are left alone — the worker re-verifies before it
    sends, and pulling the row out from under it would help nothing.

    Returns:
        How many schedules were stopped.
    """
    moment = to_iso(now_utc())
    rows = conn.execute(
        "SELECT id, status FROM publication_queue WHERE draft_id = ? AND status = 'SCHEDULED'",
        (str(draft_id),),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            UPDATE publication_queue
               SET status = 'INVALIDATED', hold_reason = ?, updated_at = ?,
                   last_checked_at = ?, claimed_by = NULL, claimed_at = NULL,
                   lease_expires_at = NULL
             WHERE id = ?
            """,
            (reason, moment, moment, row["id"]),
        )
        conn.execute(
            """
            INSERT INTO publication_queue_events (id, queue_id, event, from_status,
                                                  to_status, detail, actor, created_at)
            VALUES (?, ?, 'INVALIDATED', ?, 'INVALIDATED', ?, ?, ?)
            """,
            (str(uuid4()), row["id"], row["status"], reason, actor, moment),
        )
    return len(rows)
