"""How long an approval stays good.

Phase 8.1 named the problem this solves: an approved post sitting in a queue for three
days is an approval ageing in public. A human read a news story and said yes; by
Thursday the story may have been superseded, corrected, or made irrelevant by the thing
it was about. Publishing it then is publishing something nobody approved *now*.

Different content ages at completely different rates, so one window would be wrong for
everything. A product announcement is stale in a day. "Що таке промпт?" is as true next
year as today.

**These are editorial defaults, not measured optima.** Nothing here is derived from
engagement data — there is none yet. They are the numbers a careful editor would pick,
written in one place so they can be argued with and changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ai_news_editor.domain.enums import ContentType

#: How long after approval a post of each type may still be published without a human
#: looking at it again.
DEFAULT_FRESHNESS: dict[ContentType, timedelta] = {
    # A day. News about a product feature is a claim about the present tense, and the
    # present tense moves.
    ContentType.NEWS: timedelta(hours=36),
    # A month. A tested prompt keeps working, but the tool it was tested on ships
    # changes, and the post names that tool.
    ContentType.PROMPT: timedelta(days=30),
    # A month, for the same reason: it reports what happened when somebody ran something.
    ContentType.TESTED_USE_CASE: timedelta(days=30),
    # A year. What a prompt *is* does not change.
    ContentType.EXPLAINER: timedelta(days=365),
    # A year. A checklist stays a checklist.
    ContentType.RESOURCE: timedelta(days=365),
}

#: How late a post may be published after its scheduled time without a human looking
#: again. The Mac sleeps, the process stops, the queue backs up — and a news post that
#: was due yesterday morning is not the same post today.
DEFAULT_OVERDUE_TOLERANCE: dict[ContentType, timedelta] = {
    ContentType.NEWS: timedelta(hours=2),
    ContentType.PROMPT: timedelta(days=1),
    ContentType.TESTED_USE_CASE: timedelta(days=1),
    ContentType.EXPLAINER: timedelta(days=3),
    ContentType.RESOURCE: timedelta(days=3),
}


@dataclass(frozen=True, slots=True)
class FreshnessVerdict:
    """Whether a post may still go out, and why not if it may not."""

    fresh: bool
    reason: str | None = None
    age: timedelta | None = None

    def __bool__(self) -> bool:
        return self.fresh


def freshness_window(content_type: ContentType) -> timedelta:
    return DEFAULT_FRESHNESS.get(content_type, timedelta(days=30))


def overdue_tolerance(content_type: ContentType) -> timedelta:
    return DEFAULT_OVERDUE_TOLERANCE.get(content_type, timedelta(days=1))


def check_freshness(
    *, content_type: ContentType, approved_at: datetime, now: datetime
) -> FreshnessVerdict:
    """Is the approval still recent enough to act on?

    Measured from the approval, not from when the story was written: the question is how
    long ago a human looked at this and said yes.
    """
    age = now - approved_at
    window = freshness_window(content_type)
    if age <= window:
        return FreshnessVerdict(fresh=True, age=age)
    return FreshnessVerdict(
        fresh=False,
        age=age,
        reason=(
            f"approved {_readable(age)} ago, and {content_type.value} content is only "
            f"published within {_readable(window)} of approval. It needs another look "
            "rather than an extended approval."
        ),
    )


def check_overdue(
    *, content_type: ContentType, scheduled_for: datetime, now: datetime
) -> FreshnessVerdict:
    """Is this too late to publish unattended?

    The scenario is mundane and certain to happen: the Mac slept through the scheduled
    time. Publishing a day-old "щойно з'явилося" post because a process finally woke up
    is worse than not publishing it.
    """
    if now <= scheduled_for:
        return FreshnessVerdict(fresh=True, age=timedelta(0))
    delay = now - scheduled_for
    tolerance = overdue_tolerance(content_type)
    if delay <= tolerance:
        return FreshnessVerdict(fresh=True, age=delay)
    return FreshnessVerdict(
        fresh=False,
        age=delay,
        reason=(
            f"due {_readable(delay)} ago, past the {_readable(tolerance)} this "
            f"{content_type.value} post may be published late without a second look"
        ),
    )


def _readable(delta: timedelta) -> str:
    """A duration a human reads without converting anything."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"
