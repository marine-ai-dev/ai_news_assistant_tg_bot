"""Ephemeral interaction state for one running bot process.

Two things need remembering between updates: which draft the owner is currently editing,
and which message holds the card being acted on. Both are UI state, and both are
deliberately in memory only.

**Losing this state on restart is correct behaviour, not a limitation.** Nothing here
authorizes anything. If the process dies mid-edit, the edit simply did not happen — no
version was appended, no decision was recorded, and the database is exactly where it was.
Persisting it would create a way for a stale intention to survive a restart and act on a
draft that has since changed, which is the failure this design refuses to build.

Every action re-reads the draft from the database and re-checks the version anyway, so
this state is never the thing that makes an action safe.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

#: An edit left open longer than this is abandoned rather than applied. A message typed
#: an hour after the prompt is probably not the edit that was intended.
EDIT_TIMEOUT_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class EditIntent:
    """The owner said they want to replace the text of one exact version."""

    draft_id: UUID
    version_id: UUID
    version_no: int
    started_at: float

    def expired(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.started_at > EDIT_TIMEOUT_SECONDS


@dataclass
class Session:
    """Per-owner UI state. One owner, so one of these per process."""

    editing: EditIntent | None = None
    #: A time chosen but not yet confirmed, and the draft it was chosen for.
    scheduling: ScheduleIntent | None = None
    #: Set when the owner asked to type a custom time, so the next plain message is
    #: read as a date rather than as an edit or a stray line.
    awaiting_custom_time: tuple[UUID, int] | None = None
    #: Draft ids the owner skipped in this run. Navigation only — skipping records
    #: nothing and changes nothing, it just stops the same draft reappearing next.
    skipped: set[UUID] = field(default_factory=set)

    def begin_edit(self, draft_id: UUID, version_id: UUID, version_no: int) -> None:
        self.editing = EditIntent(
            draft_id=draft_id,
            version_id=version_id,
            version_no=version_no,
            started_at=time.monotonic(),
        )

    def active_edit(self) -> EditIntent | None:
        """The current edit intent, or None if there is none or it has gone stale."""
        if self.editing is None:
            return None
        if self.editing.expired():
            self.editing = None
            return None
        return self.editing

    def end_edit(self) -> None:
        self.editing = None

    def begin_schedule(
        self,
        draft_id: UUID,
        version_id: UUID,
        version_no: int,
        when: datetime,
        timezone_name: str,
    ) -> None:
        self.scheduling = ScheduleIntent(
            draft_id=draft_id,
            version_id=version_id,
            version_no=version_no,
            when=when,
            timezone_name=timezone_name,
            started_at=time.monotonic(),
        )
        self.awaiting_custom_time = None

    def active_schedule(self) -> ScheduleIntent | None:
        """The unconfirmed time, or None if there is none or it has gone stale."""
        if self.scheduling is None:
            return None
        if self.scheduling.expired():
            self.scheduling = None
            return None
        return self.scheduling

    def end_schedule(self) -> None:
        self.scheduling = None
        self.awaiting_custom_time = None

    def ask_for_time(self, draft_id: UUID, version_no: int) -> None:
        self.awaiting_custom_time = (draft_id, version_no)
        self.scheduling = None

    def skip(self, draft_id: UUID) -> None:
        self.skipped.add(draft_id)

    def reset_navigation(self) -> None:
        self.skipped.clear()


@dataclass(frozen=True, slots=True)
class ScheduleIntent:
    """The owner picked a time for one exact version, and has not confirmed it yet.

    Kept here rather than in ``callback_data`` for the same reason as everything else in
    this module: a callback string travelled to a client and back, and a timestamp
    carried in one would be a time the bot did not choose. This is chosen by the bot,
    held in memory, and re-verified against the database before it becomes a queue row.

    Losing it on restart is correct. An unconfirmed schedule is not a schedule.
    """

    draft_id: UUID
    version_id: UUID
    version_no: int
    #: The resolved instant, in UTC. Already validated against DST.
    when: datetime
    timezone_name: str
    started_at: float

    def expired(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.started_at > EDIT_TIMEOUT_SECONDS
