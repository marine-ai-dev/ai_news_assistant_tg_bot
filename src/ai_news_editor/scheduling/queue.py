"""Deciding whether something may be scheduled, and recording that it was.

Scheduling looks like a small feature and is not. It puts time between the moment a
human said yes and the moment a post appears, and everything that can go wrong in this
project lives in that gap: the draft gets edited, the approval is withdrawn, the image
is moved, the news stops being news.

So the answer to "may this be queued?" is the same question as "may this be published?",
asked early — and then asked again, in full, immediately before the send. Nothing here
grants permission that lasts. A queue item is a request to re-ask the question at a
particular time, not a permission slip issued in advance.

Two things this module will never do. It will never approve anything: only a draft a
human already approved can be scheduled, and scheduling is a separate act from
approving. And it will never choose what to schedule: every item here exists because
the owner asked for it by id.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import DraftStatus, QueueStatus
from ai_news_editor.domain.models import Draft, DraftVersion, QueueItem
from ai_news_editor.publishing.eligibility import publication_problem
from ai_news_editor.publishing.gate import authorization_for_approved_draft
from ai_news_editor.publishing.plan import PlanError, check_asset, publishable_media
from ai_news_editor.scheduling.clock import CHANNEL_TIMEZONE, describe
from ai_news_editor.storage.db import transaction
from ai_news_editor.storage.repositories import DraftRepository, PublicationRepository
from ai_news_editor.storage.repositories.publication_queue import PublicationQueueRepository

#: How close two posts have to be before the owner is warned. A warning, never a
#: refusal — two posts an hour apart may be exactly what a busy news day calls for.
CROWDING_WINDOW = timedelta(minutes=45)


class QueueError(Exception):
    """This cannot be scheduled, and the message says why in the owner's terms."""


@dataclass(frozen=True, slots=True)
class Schedulable:
    """An approved draft that passed every check, with what scheduling it would need."""

    draft: Draft
    version: DraftVersion
    review_decision_id: UUID
    content_hash: str


@dataclass(frozen=True, slots=True)
class SchedulingWarning:
    """Something the owner should know before confirming, but which does not block."""

    kind: str
    message: str


def check_schedulable(
    connection: sqlite3.Connection,
    draft_id: UUID,
    *,
    channel: str,
    media_root: Path,
) -> Schedulable:
    """Every reason a draft may not be scheduled, checked before a row is written.

    Creating a queue item that the scheduler will later refuse is a way of telling the
    owner at three in the morning what could have been said at the keyboard. So the full
    set is checked here — approval, evidence, provenance, files — and the same set is
    checked again at publication time, because time passes.

    Raises:
        QueueError: not approved, approval invalidated, already published, evidence
            missing, or a required file is not on disk.
    """
    drafts = DraftRepository(connection)
    draft = drafts.get(draft_id)

    if draft.status is DraftStatus.PUBLISHED:
        raise QueueError(
            f"draft {draft_id} is already published; scheduling it again would put a "
            "second copy on the channel"
        )

    # The gate, not a reimplementation of it. It returns an authorization only when a
    # human approved this exact current version and that approval still stands.
    authorization = authorization_for_approved_draft(connection, draft_id)
    if authorization is None:
        raise QueueError(
            f"draft {draft_id} is {draft.status.value} and has no valid approval for its "
            "current version. Only approved content can be scheduled — scheduling is not "
            "a way to approve something."
        )

    version = drafts.current_version(draft_id)

    # Evidence and provenance. Approval cannot override a missing demonstration, and
    # neither can a schedule.
    problem = publication_problem(connection, draft)
    if problem is not None:
        raise QueueError(problem)

    publications = PublicationRepository(connection)
    if publications.successful_for_version(version.id, channel) is not None:
        raise QueueError(
            f"this version was already published to {channel}. Nothing will be scheduled."
        )
    unresolved = publications.unresolved_for_version(version.id, channel)
    if unresolved is not None:
        raise QueueError(
            f"an earlier attempt to publish this version to {channel} ended with an "
            f"unknown outcome. Resolve publication {unresolved.id} before scheduling it."
        )

    # Files, now rather than at midnight. An approved post that promises an image and
    # cannot produce one must not become a scheduler problem.
    _assert_assets_present(version, media_root)

    queue = PublicationQueueRepository(connection)
    existing = queue.active_for_version(version.id, channel)
    if existing is not None:
        at = describe(existing.scheduled_for, existing.display_timezone)
        raise QueueError(
            f"this version is already scheduled for {at} (queue item "
            f"{str(existing.id)[:8]}). Reschedule that one rather than adding a second."
        )

    return Schedulable(
        draft=draft,
        version=version,
        review_decision_id=authorization.decision_id,
        content_hash=authorization.content_hash,
    )


def _assert_assets_present(version: DraftVersion, media_root: Path) -> None:
    """Every file the approved bundle promises must exist and be usable.

    Raises:
        QueueError: a file is missing, outside the media directory, or unusable.
    """
    for asset in publishable_media(version):
        try:
            check_asset(asset, media_root)
        except PlanError as exc:
            raise QueueError(str(exc)) from exc


def warnings_for(
    connection: sqlite3.Connection,
    when: datetime,
    *,
    exclude: UUID | None = None,
) -> list[SchedulingWarning]:
    """What to tell the owner before they confirm a time.

    Neither of these changes the schedule. Moving a post the owner asked for, without
    being asked, is worse than a crowded afternoon.
    """
    queue = PublicationQueueRepository(connection)
    found: list[SchedulingWarning] = []

    for other in queue.neighbours(when, within=CROWDING_WINDOW, exclude=exclude):
        if other.scheduled_for == when:
            found.append(
                SchedulingWarning(
                    kind="collision",
                    message=(
                        f"another post is scheduled for exactly this moment "
                        f"({describe(when, other.display_timezone)}). Two posts landing in "
                        "the same second reads as a glitch — confirm deliberately or "
                        "choose another time."
                    ),
                )
            )
        else:
            gap = abs(other.scheduled_for - when)
            found.append(
                SchedulingWarning(
                    kind="crowding",
                    message=(
                        f"another post is scheduled {int(gap.total_seconds() // 60)} min "
                        f"away ({describe(other.scheduled_for, other.display_timezone)})"
                    ),
                )
            )
    return found


def schedule(
    connection: sqlite3.Connection,
    draft_id: UUID,
    when: datetime,
    *,
    channel: str,
    media_root: Path,
    actor: str,
    timezone_name: str = CHANNEL_TIMEZONE,
    allow_collision: bool = False,
) -> tuple[QueueItem, list[SchedulingWarning]]:
    """Queue an approved version for an exact future moment.

    Raises:
        QueueError: the draft cannot be scheduled, the time is in the past, or another
            post already holds that exact moment and the caller did not say to go ahead.
    """
    if when.tzinfo is None:
        raise QueueError("a scheduled time must carry a timezone; naive datetimes are refused")
    if when <= now_utc():
        raise QueueError(
            f"{describe(when, timezone_name)} is in the past. Publishing now is the "
            "'publish' command; the queue is for later."
        )

    ready = check_schedulable(connection, draft_id, channel=channel, media_root=media_root)
    found = warnings_for(connection, when)

    if not allow_collision and any(w.kind == "collision" for w in found):
        raise QueueError(
            f"another post is already scheduled for exactly {describe(when, timezone_name)}. "
            "Choose another time, or confirm deliberately that both should go out together."
        )

    item = QueueItem(
        draft_id=ready.draft.id,
        draft_version_id=ready.version.id,
        review_decision_id=ready.review_decision_id,
        content_hash=ready.content_hash,
        channel=channel,
        scheduled_for=when,
        display_timezone=timezone_name,
    )
    queue = PublicationQueueRepository(connection)
    with transaction(connection):
        queue.add(item, actor=actor)
    return queue.get(item.id), found


def reschedule(
    connection: sqlite3.Connection,
    queue_id: UUID,
    when: datetime,
    *,
    actor: str,
    timezone_name: str = CHANNEL_TIMEZONE,
    allow_collision: bool = False,
) -> tuple[QueueItem, list[SchedulingWarning]]:
    """Move a waiting item to a different time.

    Only the new time is honoured — the row is updated, not duplicated, so there is
    never a moment when both times are live.

    Raises:
        QueueError: the time is invalid or the item is not waiting.
    """
    if when.tzinfo is None:
        raise QueueError("a scheduled time must carry a timezone; naive datetimes are refused")
    if when <= now_utc():
        raise QueueError(f"{describe(when, timezone_name)} is in the past")

    queue = PublicationQueueRepository(connection)
    item = queue.get(queue_id)
    if item.status is not QueueStatus.SCHEDULED:
        raise QueueError(
            f"queue item {str(queue_id)[:8]} is {item.status.value}. Only a waiting item "
            "can be moved; resolve this one first."
        )

    found = warnings_for(connection, when, exclude=queue_id)
    if not allow_collision and any(w.kind == "collision" for w in found):
        raise QueueError(
            f"another post is already scheduled for exactly {describe(when, timezone_name)}"
        )

    with transaction(connection):
        queue.reschedule(queue_id, when, actor=actor, timezone_name=timezone_name)
    return queue.get(queue_id), found


def cancel(
    connection: sqlite3.Connection, queue_id: UUID, *, actor: str, reason: str | None = None
) -> QueueItem:
    """Withdraw a schedule, leaving everything editorial exactly as it was.

    The draft stays approved. Cancelling a schedule is saying "not then", not "not at
    all" — the post can be scheduled again, or published by hand, without a second
    approval.

    Raises:
        QueueError: the item has already finished or is being published right now.
    """
    queue = PublicationQueueRepository(connection)
    item = queue.get(queue_id)
    if item.status is QueueStatus.PUBLISHED:
        raise QueueError("this item already published; cancelling it would change nothing")
    if item.status is QueueStatus.PROCESSING:
        raise QueueError(
            "a worker is publishing this item right now. Wait for it to finish rather "
            "than cancelling mid-send."
        )
    with transaction(connection):
        queue.set_status(
            queue_id,
            QueueStatus.CANCELLED,
            actor=actor,
            reason=reason or "withdrawn by the owner",
            event="CANCELLED",
        )
    return queue.get(queue_id)


def invalidate_for_draft(
    connection: sqlite3.Connection, draft_id: UUID, *, actor: str, reason: str
) -> list[QueueItem]:
    """Stop every live schedule for a draft whose content or approval moved on.

    Called when a new version is appended and when an approval stops applying. The item
    is never retargeted at the new version: the owner approved specific words for a
    specific time, and a new version is neither. It has to be read, approved and
    scheduled again.

    Returns the items that were stopped, so the caller can tell the owner.
    """
    queue = PublicationQueueRepository(connection)
    stopped: list[QueueItem] = []
    for item in queue.active_for_draft(draft_id):
        if item.status is QueueStatus.PROCESSING:
            # Mid-send. Leave the worker alone; it re-verifies before the send anyway,
            # and taking the row out from under it would be the only way to confuse it.
            continue
        queue.set_status(
            item.id, QueueStatus.INVALIDATED, actor=actor, reason=reason, event="INVALIDATED"
        )
        stopped.append(queue.get(item.id))
    return stopped


def upcoming(connection: sqlite3.Connection, *, limit: int = 50) -> list[QueueItem]:
    return PublicationQueueRepository(connection).list_upcoming(limit=limit)


def needing_attention(connection: sqlite3.Connection, *, limit: int = 50) -> list[QueueItem]:
    return PublicationQueueRepository(connection).list_needing_attention(limit=limit)
