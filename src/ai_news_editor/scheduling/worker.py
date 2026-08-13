"""The scheduler: deterministic infrastructure that publishes nothing on its own initiative.

Worth being precise about what this process is, because "scheduler" suggests more
autonomy than it has. It reads a queue of items the owner explicitly created, waits for
their times, re-checks every condition that made them publishable in the first place,
and calls the existing publication service. It does not choose content, does not judge
quality, does not browse anything, and does not run a model. Given the same database and
the same clock it makes the same decision every time.

The one interesting design choice is that **an approval does not travel forward in
time**. A queue item is not permission granted in advance; it is a request to ask the
question again at a particular moment. So every check that guarded the publish command
runs again here, plus two that only make sense once time has passed:

* has the content aged out of its window (:mod:`scheduling.freshness`)?
* has the moment itself gone stale, because the Mac slept through it?

Both answer with a hold rather than a publication. Every failure mode in this module
resolves the same way: stop, record why in words the owner can act on, and wait for a
human. Nothing here retries an uncertain send, extends an approval, or decides that
close enough is close enough.
"""

from __future__ import annotations

import signal
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import QueueStatus
from ai_news_editor.domain.errors import AiNewsError, PublicationOutcomeUncertainError
from ai_news_editor.domain.models import Draft, DraftVersion, QueueItem
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.observability.redaction import redact
from ai_news_editor.publishing.plan import BundlePlan, PlanError, check_asset, publishable_media
from ai_news_editor.publishing.rich import ComponentRepository
from ai_news_editor.publishing.service import prepare_publication, publish_bundle
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.scheduling.clock import describe
from ai_news_editor.scheduling.freshness import check_freshness, check_overdue
from ai_news_editor.storage.repositories import DraftRepository, ReviewDecisionRepository
from ai_news_editor.storage.repositories.publication_queue import (
    DEFAULT_LEASE,
    PublicationQueueRepository,
)

logger = get_logger(__name__)

#: How often the loop looks at the queue. Publication times are minutes, not
#: milliseconds, so a minute of latency costs nothing and a tight loop costs a laptop
#: battery. The loop also sleeps only until the next scheduled item when that is sooner.
DEFAULT_POLL_INTERVAL = timedelta(seconds=60)


class Verdict(StrEnum):
    """What the scheduler concluded about one item."""

    #: Every condition holds; this may be sent.
    PUBLISH = "PUBLISH"
    #: A person has to look. The queue item carries the reason.
    HOLD = "HOLD"
    #: The content aged out of its window.
    STALE = "STALE"
    #: What it pointed at changed; it can never publish.
    INVALIDATE = "INVALIDATE"
    #: Already on the channel. Nothing to do, and nothing sent.
    DONE = "DONE"


@dataclass(frozen=True, slots=True)
class Check:
    """One named precondition and how it came out. The dry run prints these."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Assessment:
    """Everything the scheduler decided about an item, and why.

    Produced without contacting Telegram, except for the read-only discussion-group
    lookup the plan needs. A dry run is exactly this object, printed.
    """

    item: QueueItem
    verdict: Verdict
    checks: tuple[Check, ...]
    reason: str | None = None
    draft: Draft | None = None
    version: DraftVersion | None = None
    plan: BundlePlan | None = None
    delay: timedelta | None = None

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)


@dataclass(slots=True)
class RunReport:
    """What one pass over the queue did."""

    assessed: list[Assessment] = field(default_factory=list)
    published: list[UUID] = field(default_factory=list)
    held: list[UUID] = field(default_factory=list)
    skipped_not_claimed: list[UUID] = field(default_factory=list)
    recovered: list[UUID] = field(default_factory=list)
    dry_run: bool = False

    @property
    def sends_made(self) -> int:
        return 0 if self.dry_run else len(self.published)


def assess(
    connection: sqlite3.Connection,
    item: QueueItem,
    *,
    now: datetime,
    media_root: Path,
    discussion_chat_id: int | None = None,
) -> Assessment:
    """Re-ask every question that made this schedulable, at the moment it matters.

    Ordered so the cheapest and most decisive checks come first, and so a failure
    reports the real cause rather than a consequence of it: a draft that was edited
    should say "edited", not "approval missing".

    Contacts nothing. The discussion-group id is passed in, already looked up.
    """
    checks: list[Check] = []
    drafts = DraftRepository(connection)
    decisions = ReviewDecisionRepository(connection)

    def fail(verdict: Verdict, reason: str) -> Assessment:
        return Assessment(item=item, verdict=verdict, checks=tuple(checks), reason=reason)

    # 1. The queue record itself.
    if item.status not in {QueueStatus.SCHEDULED, QueueStatus.PROCESSING}:
        checks.append(Check("queue record", False, item.status.value))
        return fail(Verdict.HOLD, f"the queue item is {item.status.value}, not waiting to publish")
    checks.append(Check("queue record", True, item.status.value))

    draft = drafts.get(item.draft_id)

    # 2. The exact version is still the draft's current one. This is the check that
    # catches an edit, and it has to come before the approval check: an edited draft has
    # no valid approval *because* it was edited, and saying so is more useful.
    if draft.current_version_id != item.draft_version_id:
        checks.append(Check("version still current", False, "the draft was edited"))
        return Assessment(
            item=item,
            verdict=Verdict.INVALIDATE,
            checks=tuple(checks),
            reason=(
                "the draft was edited after this was scheduled. The approved version is no "
                "longer current, so this item can never publish; the new version needs its "
                "own approval and its own schedule."
            ),
            draft=draft,
        )
    checks.append(Check("version still current", True))

    version = drafts.current_version(item.draft_id)

    # 3. The approved bundle still hashes to what was scheduled — text, comment, media
    # and resource together.
    if version.content_hash != item.content_hash:
        checks.append(Check("approved content unchanged", False, "content hash differs"))
        return Assessment(
            item=item,
            verdict=Verdict.INVALIDATE,
            checks=tuple(checks),
            reason=(
                "the approved content no longer hashes to what was scheduled; something "
                "about the post changed after approval"
            ),
            draft=draft,
            version=version,
        )
    checks.append(Check("approved content unchanged", True))

    # 4. The approval this schedule rests on is still the live one.
    approval = decisions.latest_approval(item.draft_id, item.draft_version_id)
    if approval is None or approval.id != item.review_decision_id:
        checks.append(Check("approval still valid", False, "no matching approval"))
        return Assessment(
            item=item,
            verdict=Verdict.INVALIDATE,
            checks=tuple(checks),
            reason=(
                "the approval this schedule rests on is no longer the current approval "
                "for this version. Nothing will be sent without a fresh human decision."
            ),
            draft=draft,
            version=version,
        )
    checks.append(Check("approval still valid", True, f"decision {str(approval.id)[:8]}"))

    # 5. Freshness. An approval that was right on Monday is not automatically right on
    # Friday, and how long "right" lasts depends entirely on what kind of post it is.
    fresh = check_freshness(
        content_type=draft.content_type, approved_at=approval.created_at, now=now
    )
    checks.append(Check("freshness", fresh.fresh, fresh.reason or _age(fresh.age)))
    if not fresh:
        return Assessment(
            item=item,
            verdict=Verdict.STALE,
            checks=tuple(checks),
            reason=fresh.reason,
            draft=draft,
            version=version,
        )

    # 6. The moment itself. The Mac slept, the process was off, the queue backed up —
    # and a post due yesterday morning is a different editorial proposition today.
    delay = max(now - item.scheduled_for, timedelta(0))
    timely = check_overdue(
        content_type=draft.content_type, scheduled_for=item.scheduled_for, now=now
    )
    checks.append(Check("overdue tolerance", timely.fresh, timely.reason or _age(delay)))
    if not timely:
        return Assessment(
            item=item,
            verdict=Verdict.STALE,
            checks=tuple(checks),
            reason=timely.reason,
            draft=draft,
            version=version,
            delay=delay,
        )

    # 7. The files. An approved bundle that promises an image must still have it; a rich
    # post arriving without its picture is not the post that was approved.
    try:
        for asset in publishable_media(version):
            check_asset(asset, media_root)
    except PlanError as exc:
        checks.append(Check("assets present", False, str(exc)))
        return Assessment(
            item=item,
            verdict=Verdict.HOLD,
            checks=tuple(checks),
            reason=str(exc),
            draft=draft,
            version=version,
            delay=delay,
        )
    checks.append(Check("assets present", True, f"{len(publishable_media(version))} file(s)"))

    # 8. Nothing left in an unknown state from a previous attempt. Retrying an uncertain
    # component is the one way to put a visible duplicate on the channel.
    unknown = ComponentRepository(connection).uncertain(version.id)
    if unknown:
        names = ", ".join(sorted(c.value for c in unknown))
        checks.append(Check("no unresolved attempt", False, names))
        return Assessment(
            item=item,
            verdict=Verdict.HOLD,
            checks=tuple(checks),
            reason=(
                f"an earlier attempt left {names} in an unknown state, so this post may "
                "already be partly on the channel. Check it by hand — nothing will be "
                "retried automatically."
            ),
            draft=draft,
            version=version,
            delay=delay,
        )
    checks.append(Check("no unresolved attempt", True))

    # 9. The gate, the evidence policy, the duplicate check and the plan, all through the
    # existing publication service. Not reimplemented: the same function the publish
    # command calls, so the scheduler cannot drift away from the interactive path.
    try:
        plan = prepare_publication(
            connection,
            item.draft_id,
            channel=item.channel,
            media_root=media_root,
            discussion_chat_id=discussion_chat_id,
        )
    except AiNewsError as exc:
        checks.append(Check("publication gate", False, str(exc)))
        return Assessment(
            item=item,
            verdict=Verdict.HOLD,
            checks=tuple(checks),
            reason=str(exc),
            draft=draft,
            version=version,
            delay=delay,
        )
    checks.append(Check("publication gate", True))

    if plan.already_published is not None:
        checks.append(
            Check("not already published", False, f"message {plan.already_published.message_id}")
        )
        return Assessment(
            item=item,
            verdict=Verdict.DONE,
            checks=tuple(checks),
            reason=(
                f"this version is already on {item.channel} as message "
                f"{plan.already_published.message_id}. Nothing was sent."
            ),
            draft=draft,
            version=version,
            plan=plan.bundle_plan,
            delay=delay,
        )
    if plan.unresolved is not None:
        checks.append(Check("not already published", False, "an earlier attempt is unresolved"))
        return Assessment(
            item=item,
            verdict=Verdict.HOLD,
            checks=tuple(checks),
            reason=(
                f"publication {plan.unresolved.id} ended with an unknown outcome and may "
                "already be on the channel. Resolve it by hand."
            ),
            draft=draft,
            version=version,
            plan=plan.bundle_plan,
            delay=delay,
        )
    checks.append(Check("not already published", True))

    return Assessment(
        item=item,
        verdict=Verdict.PUBLISH,
        checks=tuple(checks),
        draft=draft,
        version=version,
        plan=plan.bundle_plan,
        delay=delay,
    )


def _age(delta: timedelta | None) -> str:
    if delta is None:
        return ""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min late" if minutes else "on time"
    return f"{minutes // 60}h {minutes % 60} min late"


#: Builds a Telegram client when one is actually needed. A factory rather than a client,
#: so a dry run never opens a connection at all.
ClientFactory = Callable[[], TelegramClient]


def process_once(
    connection: sqlite3.Connection,
    *,
    worker: str,
    channel: str,
    media_root: Path,
    client_factory: ClientFactory | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    lease: timedelta = DEFAULT_LEASE,
    limit: int = 20,
) -> RunReport:
    """One deterministic pass: claim what is due, decide, and act on the decision.

    The order matters. Claiming comes first, so two workers cannot both assess the same
    item and both conclude "publish". Assessment comes second, on an item this worker
    provably owns. Publication comes last, through the ordinary publication service.

    A dry run does everything except claim and send: it assesses each due item and
    reports what a real pass would do. Since the assessment is the same function, the
    report is not an approximation.
    """
    moment = now or now_utc()
    queue = PublicationQueueRepository(connection)
    report = RunReport(dry_run=dry_run)

    recovered = _recover_stale_claims(queue, worker=worker, now=moment)
    report.recovered.extend(recovered)

    discussion_chat_id = _discussion_chat(client_factory, channel) if client_factory else None

    for candidate in queue.due(now=moment, limit=limit):
        if dry_run:
            report.assessed.append(
                assess(
                    connection,
                    candidate,
                    now=moment,
                    media_root=media_root,
                    discussion_chat_id=discussion_chat_id,
                )
            )
            continue

        claimed = queue.claim(candidate.id, worker=worker, now=moment, lease=lease)
        if claimed is None:
            # Another worker got there first. Not an error — this is the mechanism
            # working, and the correct response is to move on without a word to Telegram.
            report.skipped_not_claimed.append(candidate.id)
            logger.info("queue item claimed elsewhere", extra={"queue_id": str(candidate.id)})
            continue

        assessment = assess(
            connection,
            claimed,
            now=moment,
            media_root=media_root,
            discussion_chat_id=discussion_chat_id,
        )
        report.assessed.append(assessment)
        _act(connection, queue, assessment, worker=worker, media_root=media_root,
             client_factory=client_factory, report=report)

    return report


def _act(
    connection: sqlite3.Connection,
    queue: PublicationQueueRepository,
    assessment: Assessment,
    *,
    worker: str,
    media_root: Path,
    client_factory: ClientFactory | None,
    report: RunReport,
) -> None:
    """Carry out one assessment. Every branch except PUBLISH ends in a human's hands."""
    item = assessment.item

    if assessment.verdict is Verdict.INVALIDATE:
        queue.set_status(
            item.id, QueueStatus.INVALIDATED, actor=worker,
            reason=assessment.reason, event="INVALIDATED",
        )
        report.held.append(item.id)
        return
    if assessment.verdict is Verdict.STALE:
        queue.set_status(
            item.id, QueueStatus.STALE_REVIEW_REQUIRED, actor=worker,
            reason=assessment.reason, event="STALE",
        )
        report.held.append(item.id)
        return
    if assessment.verdict is Verdict.HOLD:
        queue.set_status(
            item.id, QueueStatus.HOLD_FOR_REVIEW, actor=worker,
            reason=assessment.reason, event="HELD",
        )
        report.held.append(item.id)
        return
    if assessment.verdict is Verdict.DONE:
        queue.set_status(
            item.id, QueueStatus.PUBLISHED, actor=worker,
            reason=assessment.reason, event="ALREADY_PUBLISHED",
        )
        return

    if client_factory is None:  # pragma: no cover - callers always supply one to publish
        queue.release(item.id, worker=worker, reason="no Telegram client configured")
        return

    _publish(connection, queue, assessment, worker=worker, media_root=media_root,
             client_factory=client_factory, report=report)


def _publish(
    connection: sqlite3.Connection,
    queue: PublicationQueueRepository,
    assessment: Assessment,
    *,
    worker: str,
    media_root: Path,
    client_factory: ClientFactory,
    report: RunReport,
) -> None:
    """Send, through the same service the publish command uses.

    Telegram sending is not reimplemented here and must not be. Everything that makes a
    bundle safe — the plan, the component history, the never-resend-the-post rule, the
    deferred comment — lives in the publication service, and the scheduler is one more
    caller of it.
    """
    item = assessment.item
    try:
        with client_factory() as client:
            plan = prepare_publication(
                connection,
                item.draft_id,
                channel=item.channel,
                media_root=media_root,
                discussion_chat_id=_linked_discussion(client, item.channel),
            )
            publication = publish_bundle(connection, plan, client, media_root=media_root)
    except PublicationOutcomeUncertainError as exc:
        # The dangerous one. Never retried by a machine.
        queue.set_status(
            item.id, QueueStatus.UNCERTAIN, actor=worker,
            reason=(
                f"{redact(str(exc))} Nothing will be retried automatically — check the "
                "channel and resolve it by hand."
            ),
            event="UNCERTAIN",
        )
        report.held.append(item.id)
        logger.error("scheduled publication outcome unknown", extra={"queue_id": str(item.id)})
        return
    except Exception as exc:
        queue.set_status(
            item.id, QueueStatus.FAILED, actor=worker,
            reason=redact(str(exc)), event="FAILED",
        )
        report.held.append(item.id)
        logger.error(
            "scheduled publication failed",
            extra={"queue_id": str(item.id), "error": type(exc).__name__},
        )
        return

    queue.set_status(
        item.id, QueueStatus.PUBLISHED, actor=worker,
        reason=None, event="PUBLISHED", publication_id=publication.id,
    )
    report.published.append(item.id)
    logger.info(
        "scheduled publication sent",
        extra={"queue_id": str(item.id), "message_id": publication.message_id},
    )


def _recover_stale_claims(
    queue: PublicationQueueRepository, *, worker: str, now: datetime
) -> list[UUID]:
    """Release items a dead worker was holding.

    A crashed process must not lock a post out of the channel forever, and it must not
    cause a duplicate either. Both hold, and for a reason that is worth stating plainly:
    releasing an item grants nothing. The item goes back to SCHEDULED and is assessed
    from scratch, and the publication service — which reads what was actually sent, not
    what a lease says — decides what may still go out. If the dead worker had already
    moved the draft to PUBLISHING, the assessment finds no valid approval and holds it
    for a person, which is the right answer when nobody knows what was sent.
    """
    released: list[UUID] = []
    for item in queue.stale_claims(now=now):
        queue.release(
            item.id,
            worker=worker,
            reason=(
                f"the lease held by {item.claimed_by or 'a previous worker'} expired; "
                "reassessing from scratch"
            ),
        )
        released.append(item.id)
        logger.warning("recovered an expired queue claim", extra={"queue_id": str(item.id)})
    return released


def _discussion_chat(client_factory: ClientFactory, channel: str) -> int | None:
    """Read-only lookup, so a deferred comment is visible in a dry run."""
    try:
        with client_factory() as client:
            return _linked_discussion(client, channel)
    except Exception:  # pragma: no cover - a network problem must not stop assessment
        return None


def _linked_discussion(client: TelegramClient, channel: str) -> int | None:
    try:
        return client.linked_discussion_chat(channel)
    except AiNewsError:
        return None


@contextmanager
def _stop_on_signal() -> Iterator[threading.Event]:
    """Ctrl-C and SIGTERM stop the loop between passes, never mid-send.

    A scheduler killed halfway through a send is exactly the situation the component
    history exists for, but not creating it in the first place is better.
    """
    stop = threading.Event()
    previous: dict[int, object] = {}

    def handle(signum: int, _frame: object) -> None:
        logger.info("scheduler stopping", extra={"signal": signum})
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not the main thread: nothing to install, and nothing lost — the caller's own
        # shutdown handling still applies.
        with suppress(ValueError):  # pragma: no cover
            previous[sig] = signal.signal(sig, handle)
    try:
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]


def run(
    connection: sqlite3.Connection,
    *,
    worker: str,
    channel: str,
    media_root: Path,
    client_factory: ClientFactory | None = None,
    poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
    on_pass: Callable[[RunReport], None] | None = None,
    max_passes: int | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Poll the queue until stopped. One pass per interval, no busy waiting.

    ``max_passes`` and ``sleep`` exist so the loop is testable without waiting in real
    time; nothing else in the loop knows the difference.

    Returns:
        How many passes were made.
    """
    import time

    wait = sleep or time.sleep
    queue = PublicationQueueRepository(connection)
    passes = 0

    with _stop_on_signal() as stop:
        while not stop.is_set():
            report = process_once(
                connection,
                worker=worker,
                channel=channel,
                media_root=media_root,
                client_factory=client_factory,
            )
            passes += 1
            if on_pass is not None:
                on_pass(report)
            if max_passes is not None and passes >= max_passes:
                break

            # Sleep until the next item is due, or one interval, whichever is sooner.
            # A queue with nothing in it for six hours should not wake up 360 times.
            delay = poll_interval.total_seconds()
            upcoming = queue.next_scheduled()
            if upcoming is not None:
                until = (upcoming.scheduled_for - now_utc()).total_seconds()
                delay = max(1.0, min(delay, until))
            wait(delay)

    return passes


def format_assessment(assessment: Assessment) -> list[str]:
    """The dry run's explanation of one item, as lines.

    Deliberately verbose about the *decision*: the point of a dry run is that a person
    can see which condition would have stopped a post, not merely that one did.
    """
    item = assessment.item
    lines = [
        f"Queue item:  {str(item.id)[:8]}  ({item.status.value})",
        f"Draft:       {str(item.draft_id)[:8]}  version {str(item.draft_version_id)[:8]}",
        f"Scheduled:   {describe(item.scheduled_for, item.display_timezone)}",
    ]
    if assessment.delay is not None and assessment.delay > timedelta(0):
        lines.append(f"Delay:       {_age(assessment.delay)}")
    lines.append("")
    for check in assessment.checks:
        mark = "PASS" if check.passed else "FAIL"
        detail = f"  — {check.detail}" if check.detail else ""
        lines.append(f"  {mark}  {check.name}{detail}")
    lines.append("")
    lines.append(f"Verdict:     {assessment.verdict.value}")
    if assessment.reason:
        lines.append(f"Reason:      {assessment.reason}")
    if assessment.plan is not None:
        lines.append("")
        lines.append("Publication plan:")
        for index, step in enumerate(assessment.plan.steps, start=1):
            lines.append(f"  {index}. {step.component.value}: {step.method} — {step.summary}")
        for component, why in assessment.plan.deferred:
            lines.append(f"  —  {component.value}: DEFERRED — {why}")
    return lines
