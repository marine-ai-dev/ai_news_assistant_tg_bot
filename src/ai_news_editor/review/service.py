"""Review actions, independent of how a human triggers them.

The CLI in Phase 6 and the private Telegram review bot planned later are two front ends
over the same functions. None of the rules live in a keypress handler: a Telegram
callback will get the identical behaviour by calling the same service.

Approval is the exception — it lives in :mod:`publishing.gate`, because issuing a
publication authorization deserves to sit next to the thing it authorizes rather than
among the ordinary review verbs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import UUID

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import Category, ContentType, DraftStatus, ReviewAction
from ai_news_editor.domain.errors import AiNewsError, RepositoryError
from ai_news_editor.domain.models import (
    Article,
    ContentItem,
    Draft,
    DraftVersion,
    Evaluation,
    ReviewDecision,
)
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.publishing.gate import DEFAULT_ACTOR, MAX_NOTE_CHARS
from ai_news_editor.storage.db import transaction
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    ContentItemRepository,
    DraftRepository,
    EvaluationRepository,
    ReviewDecisionRepository,
)
from ai_news_editor.writing.format import (
    check_length,
    disallowed_tags,
    hard_limit_problem,
    render_post,
    render_version,
    source_label_of,
    source_url_of,
)

logger = get_logger(__name__)

#: Statuses a human can still act on. REJECTED and PUBLISHED are terminal; APPROVED is
#: already decided and is shown separately rather than re-offered in the queue.
REVIEWABLE: frozenset[DraftStatus] = frozenset({DraftStatus.PENDING_REVIEW})


class ReviewError(AiNewsError):
    """A review action could not be applied."""


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """Everything a reviewer needs on screen, gathered in one read."""

    draft: Draft
    version: DraftVersion
    #: The source article — news only. Editorial-original content has none, and this
    #: being None is the honest representation of that.
    article: Article | None
    evaluation: Evaluation | None
    #: The prompt or explainer this was written from. News has none.
    content_item: ContentItem | None = None

    @property
    def rendered_post(self) -> str:
        """The post exactly as it would be sent.

        Same renderer the publisher uses. A reviewer must never approve one string
        while the channel receives another.
        """
        return render_version(self.version)

    @property
    def score(self) -> float | None:
        return self.evaluation.composite_score if self.evaluation else None

    @property
    def subject(self) -> str | None:
        """The prompt topic or the explainer concept, for the review screen."""
        return self.content_item.subject if self.content_item else None


def review_queue(
    connection: sqlite3.Connection,
    *,
    draft_id: UUID | None = None,
    category: Category | None = None,
    limit: int = 100,
) -> list[ReviewItem]:
    """Drafts awaiting a human, best-scoring first.

    Ordered by editorial score so the strongest stories are read while attention is
    freshest, not whichever happened to be written first.
    """
    drafts = DraftRepository(connection)
    articles = ArticleRepository(connection)
    evaluations = EvaluationRepository(connection)
    content_items = ContentItemRepository(connection)

    if draft_id is not None:
        candidates = [drafts.get(draft_id)]
    else:
        candidates = drafts.list_by_status(DraftStatus.PENDING_REVIEW, limit=limit)

    items: list[ReviewItem] = []
    for draft in candidates:
        if draft.status not in REVIEWABLE:
            continue
        version = drafts.current_version(draft.id)
        if category is not None and version.category is not category:
            continue
        items.append(
            ReviewItem(
                draft=draft,
                version=version,
                article=articles.get(draft.article_id) if draft.article_id else None,
                evaluation=(
                    evaluations.latest_for_article(draft.article_id)
                    if draft.article_id
                    else None
                ),
                content_item=(
                    content_items.get(draft.content_item_id)
                    if draft.content_item_id
                    else None
                ),
            )
        )

    items.sort(key=lambda item: (item.score is None, -(item.score or 0)))
    return items


def reject_draft(
    connection: sqlite3.Connection,
    draft_id: UUID,
    *,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    expected_version_id: UUID | None = None,
) -> Draft:
    """Reject a draft. Terminal, recorded, and never deleted.

    A rejected draft leaves the review queue but stays in the database with its whole
    history: the article and its evaluation are untouched, and the record of what was
    written and why it was turned down survives.
    """
    return _record_and_transition(
        connection,
        draft_id,
        action=ReviewAction.REJECT,
        target=DraftStatus.REJECTED,
        actor=actor,
        note=note,
        expected_version_id=expected_version_id,
    )


def request_rewrite(
    connection: sqlite3.Connection,
    draft_id: UUID,
    *,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    expected_version_id: UUID | None = None,
) -> Draft:
    """Send a draft back to be rewritten.

    For a post that is inaccurate, badly angled or simply weak. The note is the useful
    part — it is what a later writing pass has to work from.
    """
    return _record_and_transition(
        connection,
        draft_id,
        action=ReviewAction.REQUEST_REWRITE,
        target=DraftStatus.NEEDS_REWRITE,
        actor=actor,
        note=note,
        expected_version_id=expected_version_id,
    )


def apply_edit(
    connection: sqlite3.Connection,
    draft_id: UUID,
    *,
    headline: str,
    body: str,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    expected_version_id: UUID | None = None,
) -> tuple[Draft, DraftVersion]:
    """Append an edited version and return the draft to review.

    Version N is never touched. The edit becomes version N+1 with its own hash, which is
    what makes an earlier approval stop applying: a human approved a version, and this
    is a different one.

    Editorial text only. The article, the evaluation link, the source and the draft
    identity are not editable here — an edit revises the writing, not what the writing
    is about.

    Raises:
        ReviewError: the draft cannot be edited, or the edited text is not publishable.
    """
    drafts = DraftRepository(connection)
    decisions = ReviewDecisionRepository(connection)

    draft = drafts.get(draft_id)
    if draft.current_version_id is None:
        raise ReviewError(f"draft {draft_id} has no version to edit")
    current = drafts.current_version(draft_id)
    if expected_version_id is not None and current.id != expected_version_id:
        raise ReviewError(
            f"draft {draft_id} moved to version {current.version_no} while it was open; "
            "re-read it before editing"
        )

    problem = validate_edit(
        headline=headline,
        body=body,
        source_label=source_label_of(current.source_attribution),
        source_url=source_url_of(current),
        require_source=draft.content_type is ContentType.NEWS,
    )
    if problem:
        raise ReviewError(problem)

    try:
        with transaction(connection):
            decisions.add(
                ReviewDecision(
                    draft_id=draft.id,
                    draft_version_id=current.id,
                    content_hash=current.content_hash,
                    action=ReviewAction.EDIT,
                    actor=actor,
                    note=_clean(note),
                    created_at=now_utc(),
                )
            )
    except RepositoryError as exc:  # pragma: no cover - defensive
        raise ReviewError(str(exc)) from exc

    # append_version enforces the lifecycle itself: an APPROVED draft returns to
    # PENDING_REVIEW here, which is how editing invalidates an approval at the storage
    # layer rather than by anyone remembering to do it.
    updated, version = drafts.append_version(
        draft_id,
        title=headline.strip(),
        body=body.strip(),
        category=current.category,
        audience=current.audience,
        source_attribution=current.source_attribution,
        source_url=current.source_url,
        post_format=current.post_format,
        style_version=current.style_version,
        writer_notes=current.writer_notes,
        hashtags=current.hashtags,
        created_by=f"human:{actor}",
    )

    # A draft that was NEEDS_REWRITE lands in DRAFTED after an append; it has just been
    # rewritten by hand, so it belongs back in the queue rather than out of sight.
    if updated.status is DraftStatus.DRAFTED:
        updated = drafts.set_status(draft_id, DraftStatus.PENDING_REVIEW)

    logger.info(
        "draft edited",
        extra={
            "draft_id": str(draft_id),
            "new_version": version.version_no,
            "status": updated.status.value,
        },
    )
    return updated, version


def validate_edit(
    *,
    headline: str,
    body: str,
    source_label: str,
    source_url: str,
    require_source: bool = True,
) -> str | None:
    """Check edited text against the same rules a written draft must satisfy.

    Returns a problem description, or ``None`` if the text is publishable. Nothing is
    corrected or shortened automatically — a human edit that breaks a limit is reported
    back to the human.

    ``require_source`` is False only for editorial-original content, which has no source
    to lose. News keeps the requirement: an edit must not be able to strip the link that
    lets a reader check the claim.
    """
    if not headline.strip():
        return "the headline is empty"
    if not body.strip():
        return "the body is empty"
    if require_source and not source_url.strip():
        return "the source URL is missing; a news post must stay traceable"

    for field_name, text in (("headline", headline), ("body", body)):
        bad = disallowed_tags(text)
        if bad:
            listed = ", ".join(sorted(bad))
            return f"{field_name} uses markup outside the permitted subset: {listed}"

    try:
        rendered = render_post(
            headline=headline, body=body, source_label=source_label, source_url=source_url
        )
    except ValueError as exc:
        return str(exc)
    return hard_limit_problem(rendered)


def length_note(item: ReviewItem) -> str | None:
    """How the post compares to its format target, if it declares one."""
    if item.version.post_format is None:
        return None
    return check_length(item.rendered_post, item.version.post_format).note


def review_history(connection: sqlite3.Connection, draft_id: UUID) -> list[ReviewDecision]:
    """Every recorded human action on a draft, oldest first."""
    return ReviewDecisionRepository(connection).list_for_draft(draft_id)


def status_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Draft counts by status, for the review status view."""
    return DraftRepository(connection).count_by_status()


def _record_and_transition(
    connection: sqlite3.Connection,
    draft_id: UUID,
    *,
    action: ReviewAction,
    target: DraftStatus,
    actor: str,
    note: str | None,
    expected_version_id: UUID | None,
) -> Draft:
    """Record a decision and move the draft, atomically.

    Same transaction discipline as approval: a decision without the status change, or a
    status change nobody is recorded as having made, are both corrupt.
    """
    drafts = DraftRepository(connection)
    decisions = ReviewDecisionRepository(connection)

    draft = drafts.get(draft_id)
    if draft.status not in REVIEWABLE:
        raise ReviewError(
            f"draft {draft_id} is {draft.status.value}; only a draft awaiting review can be "
            f"{action.value.lower().replace('_', ' ')}d"
        )
    if draft.current_version_id is None:
        raise ReviewError(f"draft {draft_id} has no version to act on")

    version = drafts.current_version(draft_id)
    if expected_version_id is not None and version.id != expected_version_id:
        raise ReviewError(
            f"draft {draft_id} moved to version {version.version_no} while it was being "
            "reviewed; re-read it first"
        )

    with transaction(connection):
        decisions.add(
            ReviewDecision(
                draft_id=draft.id,
                draft_version_id=version.id,
                content_hash=version.content_hash,
                action=action,
                actor=actor,
                note=_clean(note),
                created_at=now_utc(),
            )
        )
        drafts.set_status(draft.id, target)

    logger.info(
        "review decision recorded",
        extra={"draft_id": str(draft_id), "action": action.value, "actor": actor},
    )
    return drafts.get(draft_id)


def _clean(note: str | None) -> str | None:
    if note is None:
        return None
    trimmed = " ".join(note.split())
    return trimmed[:MAX_NOTE_CHARS] or None
