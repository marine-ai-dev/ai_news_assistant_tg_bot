"""Publishing one approved draft version, once.

Everything hard about this phase is here, and all of it comes from one fact:

    **SQLite and Telegram are not in the same transaction.**

A local commit can succeed while the send fails; a send can succeed while the local
commit fails; and a send can succeed while the response is lost, leaving the local side
with no way to know. There is no clever protocol that removes this — Telegram's
sendMessage has no idempotency key to offer — so the design does the next best thing:

* it narrows the window (mark PUBLISHING, send, record) so an interrupted run is always
  found in a state that blocks a second attempt rather than inviting one;
* it records every attempt, including the ones that ended in ignorance;
* and it refuses to guess. An attempt whose outcome is unknown leaves the draft in
  PUBLISHING and stops. A human looks at the channel and resolves it.

The rule at the top of all of it: no approval, no Telegram request. The gate is
consulted first, from persisted state, and a publisher is never even constructed for a
draft that fails it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ai_news_editor.domain.authorization import PublishAuthorization
from ai_news_editor.domain.clock import UtcDatetime, now_utc
from ai_news_editor.domain.enums import DraftStatus, PublicationStatus
from ai_news_editor.domain.errors import (
    NotApprovedError,
    PublicationAlreadyExistsError,
    PublicationOutcomeUncertainError,
)
from ai_news_editor.domain.models import Draft, DraftVersion, Publication
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.observability.redaction import redact
from ai_news_editor.publishing.base import Publisher
from ai_news_editor.publishing.gate import authorization_for_approved_draft, verify_publication
from ai_news_editor.publishing.message import TelegramMessage, build_message
from ai_news_editor.publishing.plan import BundlePlan, Component, build_plan
from ai_news_editor.publishing.rich import ComponentRepository, execute, run_step
from ai_news_editor.publishing.telegram import TelegramClient
from ai_news_editor.storage.db import transaction
from ai_news_editor.storage.repositories import DraftRepository, PublicationRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    """Everything checked and assembled, with nothing sent yet.

    Produced by :func:`prepare_publication`, which performs every check that can be
    performed offline. The preview a human confirms is built from this, so what is
    approved on screen is what the payload already contains.
    """

    draft: Draft
    version: DraftVersion
    authorization: PublishAuthorization
    message: TelegramMessage
    channel: str
    already_published: Publication | None = None
    unresolved: Publication | None = None
    #: The ordered Telegram calls this bundle needs. Built before anything is sent, so
    #: a dry run shows exactly what a real run would do.
    bundle_plan: BundlePlan | None = None
    #: The linked discussion group, when the channel has one. None means a comment
    #: cannot be posted and is deferred rather than folded into the post.
    discussion_chat_id: int | None = None


def prepare_publication(
    connection: sqlite3.Connection,
    draft_id: UUID,
    *,
    channel: str,
    media_root: Path | None = None,
    discussion_chat_id: int | None = None,
) -> PublicationPlan:
    """Validate everything and build the payload, without contacting Telegram.

    This is the whole of ``--dry-run``, and also the first half of a real publish, so a
    dry run exercises the identical path rather than an approximation of it.

    Raises:
        NotApprovedError: no valid human approval covers the draft's current version.
        ApprovalInvalidatedError: the approval no longer applies.
        MessageTooLongError: the approved post will not fit in one message.
    """
    authorization = authorization_for_approved_draft(connection, draft_id)
    if authorization is None:
        draft = DraftRepository(connection).get(draft_id)
        raise NotApprovedError(
            f"draft {draft_id} is {draft.status.value} and has no valid approval for its "
            "current version; nothing will be sent to Telegram"
        )

    # Re-verify against live state. authorization_for_approved_draft built it a moment
    # ago, but verify_publication is the check the publisher path always runs, and
    # running it here means the dry run proves the same thing a real send would.
    draft, version = verify_publication(connection, authorization)

    message = build_message(version)
    # The payload is derived from the version; this asserts it is derived from the
    # *approved* version. A mismatch here would mean the renderer and the hash disagree.
    if authorization.content_hash != version.content_hash:  # pragma: no cover - defensive
        raise NotApprovedError("the approved content hash does not match the version being sent")

    # Every Telegram call this bundle needs, worked out before any of them is made.
    # Missing or unusable files raise here, so a bundle fails whole rather than
    # arriving in pieces.
    bundle_plan = build_plan(
        version,
        media_root=media_root or Path("media"),
        discussion_available=discussion_chat_id is not None,
    )

    publications = PublicationRepository(connection)
    return PublicationPlan(
        draft=draft,
        version=version,
        authorization=authorization,
        message=message,
        channel=channel,
        already_published=publications.successful_for_version(version.id, channel),
        unresolved=publications.unresolved_for_version(version.id, channel),
        bundle_plan=bundle_plan,
        discussion_chat_id=discussion_chat_id,
    )


def publish_draft(
    connection: sqlite3.Connection,
    plan: PublicationPlan,
    publisher: Publisher,
) -> Publication:
    """Send a prepared plan and record what happened.

    The sequence is deliberate:

    1. refuse if this exact version already succeeded, or if an earlier attempt's
       outcome is still unknown;
    2. move the draft to PUBLISHING and commit — so a crash during the send leaves a
       state that blocks a second attempt instead of inviting one;
    3. send;
    4. record the attempt and move the draft to its final state.

    Raises:
        PublicationAlreadyExistsError: this version already reached this destination.
        PublicationOutcomeUncertainError: the send may or may not have landed. The
            attempt is recorded as UNCERTAIN and the draft stays in PUBLISHING.
        TelegramError: a definite failure. Recorded as FAILED; the draft returns to
            APPROVED so a human can retry without re-approving.
    """
    version = plan.version
    channel = plan.channel
    drafts = DraftRepository(connection)
    publications = PublicationRepository(connection)

    if plan.already_published is not None:
        raise PublicationAlreadyExistsError(
            f"draft version {version.version_no} was already published to {channel} as "
            f"message {plan.already_published.message_id}. Nothing was sent."
        )
    if plan.unresolved is not None:
        raise PublicationOutcomeUncertainError(
            f"an earlier attempt to publish this version to {channel} ended with an "
            "unknown outcome, so it may already be posted. Check the channel and resolve "
            f"publication {plan.unresolved.id} before trying again."
        )

    # Re-verify against live state, here rather than only in prepare_publication. A
    # human has been reading a preview and typing a confirmation word since the plan was
    # built; in that window the draft could have been edited or rejected from another
    # terminal. This is the last check before anything leaves the machine, and it is the
    # same check the gate runs for every publisher.
    verify_publication(connection, plan.authorization)

    decision_id = plan.authorization.decision_id
    attempt_no = publications.next_attempt_no(version.id, channel)

    with transaction(connection):
        drafts.set_status(plan.draft.id, DraftStatus.PUBLISHING)

    logger.info(
        "sending to telegram",
        extra={
            "draft_id": str(plan.draft.id),
            "draft_version_id": str(version.id),
            "channel": channel,
            "attempt": attempt_no,
        },
    )

    try:
        receipt = publisher.publish(version, plan.authorization)
    except PublicationOutcomeUncertainError as exc:
        # The dangerous case. Record it, leave the draft in PUBLISHING — which is not a
        # publishable state — and stop. Nothing is retried and nothing is assumed.
        _record(
            publications,
            plan,
            decision_id=decision_id,
            status=PublicationStatus.UNCERTAIN,
            attempt_no=attempt_no,
            failure_reason=redact(str(exc)),
        )
        logger.error(
            "telegram publication outcome unknown",
            extra={
                "draft_id": str(plan.draft.id),
                "channel": channel,
                "attempt": attempt_no,
            },
        )
        raise
    except Exception as exc:
        # A definite failure: the request either never arrived or Telegram refused it.
        # Nothing is on the channel, so the draft can safely go back to where it was.
        _record(
            publications,
            plan,
            decision_id=decision_id,
            status=PublicationStatus.FAILED,
            attempt_no=attempt_no,
            failure_reason=redact(str(exc)),
        )
        with transaction(connection):
            # Through PUBLISH_FAILED rather than straight back, so the lifecycle
            # records that a send was attempted and failed. It does not rest there:
            # a retry must not require a second human approval, and only an APPROVED
            # draft can obtain an authorization. The failure itself is permanent in
            # the publications table, which is where an audit should look for it.
            drafts.set_status(plan.draft.id, DraftStatus.PUBLISH_FAILED)
            drafts.set_status(plan.draft.id, DraftStatus.APPROVED)
        logger.error(
            "telegram publication failed",
            extra={
                "draft_id": str(plan.draft.id),
                "channel": channel,
                "attempt": attempt_no,
                "error": type(exc).__name__,
            },
        )
        raise

    published_at = receipt.published_at or now_utc()
    with transaction(connection):
        publication = _record(
            publications,
            plan,
            decision_id=decision_id,
            status=PublicationStatus.SUCCEEDED,
            attempt_no=attempt_no,
            message_id=int(receipt.external_id) if receipt.external_id else None,
            chat_id=receipt.target,
            published_at=published_at,
        )
        drafts.set_status(plan.draft.id, DraftStatus.PUBLISHED)

    logger.info(
        "published",
        extra={
            "draft_id": str(plan.draft.id),
            "draft_version_id": str(version.id),
            "channel": channel,
            "message_id": publication.message_id,
        },
    )
    return publication


def _record(
    publications: PublicationRepository,
    plan: PublicationPlan,
    *,
    decision_id: UUID,
    status: PublicationStatus,
    attempt_no: int,
    message_id: int | None = None,
    chat_id: str | None = None,
    failure_reason: str | None = None,
    published_at: UtcDatetime | None = None,
) -> Publication:
    return publications.add(
        Publication(
            draft_id=plan.draft.id,
            draft_version_id=plan.version.id,
            review_decision_id=decision_id,
            content_hash=plan.version.content_hash,
            channel=plan.channel,
            status=status,
            message_id=message_id,
            chat_id=chat_id,
            attempt_no=attempt_no,
            failure_reason=failure_reason,
            published_at=published_at,
        )
    )


def publication_history(connection: sqlite3.Connection, draft_id: UUID) -> list[Publication]:
    """Every attempt made for one draft."""
    return PublicationRepository(connection).list_for_draft(draft_id)


def recent_publications(connection: sqlite3.Connection, limit: int = 50) -> list[Publication]:
    """The publication log, newest first."""
    return PublicationRepository(connection).list_recent(limit)


def approved_drafts(connection: sqlite3.Connection, limit: int = 50) -> list[Draft]:
    """Drafts a human has approved and which are therefore publishable."""
    return DraftRepository(connection).list_by_status(DraftStatus.APPROVED, limit=limit)



def publish_bundle(
    connection: sqlite3.Connection,
    plan: PublicationPlan,
    client: TelegramClient,
    *,
    media_root: Path | None = None,
) -> Publication:
    """Send a whole bundle — post, images, comment, file — and record every part.

    :func:`publish_draft` sends one message and is the whole story for a text-only post.
    A rich bundle is several Telegram calls with no transaction across them, so the
    sequencing here is different in one specific way:

        **the publications row is written from the outcome of the main message, before
        the remaining components are attempted.**

    That order is forced and it is also correct. ``publications`` is append-only, so its
    row cannot be written provisionally and corrected later; and component rows reference
    it, so it has to exist before any of them. Writing it from the main message is the
    honest reading — the main message *is* whether the post exists. A comment that fails
    afterwards is recorded as a failed component of a publication that succeeded, which
    is exactly what happened.

    Everything after the main message goes through :func:`rich.execute`, which skips any
    component already recorded as succeeded. That is what makes a retry safe: the main
    post is on record, so it is never sent twice.

    Raises:
        PublicationAlreadyExistsError: this version already reached this destination.
        PublicationOutcomeUncertainError: a send's result was lost. Recorded, and the
            draft stays in PUBLISHING for a human to resolve.
        TelegramError: a definite failure.
    """
    version = plan.version
    channel = plan.channel
    drafts = DraftRepository(connection)
    publications = PublicationRepository(connection)
    components = ComponentRepository(connection)

    if plan.already_published is not None:
        raise PublicationAlreadyExistsError(
            f"draft version {version.version_no} was already published to {channel} as "
            f"message {plan.already_published.message_id}. Nothing was sent."
        )
    if plan.unresolved is not None:
        raise PublicationOutcomeUncertainError(
            f"an earlier attempt to publish this version to {channel} ended with an "
            "unknown outcome, so it may already be posted. Check the channel and resolve "
            f"publication {plan.unresolved.id} before trying again."
        )

    # The last check before anything leaves the machine, against live state.
    verify_publication(connection, plan.authorization)

    bundle = plan.bundle_plan
    if bundle is None:  # pragma: no cover - prepare_publication always builds one
        raise ValueError("a bundle publication needs a plan")

    root = media_root or Path("media")
    already = components.succeeded(version.id)
    unknown = components.uncertain(version.id)
    if unknown:
        raise PublicationOutcomeUncertainError(
            f"an earlier attempt left {', '.join(sorted(c.value for c in unknown))} in an "
            "unknown state; this bundle may already be partly published. Check the channel."
        )

    main_step = bundle.step_for(Component.MAIN)
    if main_step is None:  # pragma: no cover - every plan has a main message
        raise ValueError("a bundle plan must contain a main message")

    decision_id = plan.authorization.decision_id
    attempt_no = publications.next_attempt_no(version.id, channel)

    if Component.MAIN in already:
        # A resume. The post is on the channel; only the missing parts are attempted,
        # and the existing publications row is the one they belong to.
        publication = publications.successful_for_version(version.id, channel)
        if publication is None:  # pragma: no cover - a succeeded MAIN implies a row
            raise PublicationOutcomeUncertainError(
                "the main message is recorded as sent but the publication row is missing; "
                "resolve this by hand rather than sending anything"
            )
    else:
        with transaction(connection):
            drafts.set_status(plan.draft.id, DraftStatus.PUBLISHING)

        logger.info(
            "sending bundle to telegram",
            extra={
                "draft_id": str(plan.draft.id),
                "draft_version_id": str(version.id),
                "channel": channel,
                "components": len(bundle.steps),
                "attempt": attempt_no,
            },
        )

        try:
            outcome = run_step(client, main_step, channel, root, None)
        except PublicationOutcomeUncertainError as exc:
            _record(
                publications,
                plan,
                decision_id=decision_id,
                status=PublicationStatus.UNCERTAIN,
                attempt_no=attempt_no,
                failure_reason=redact(str(exc)),
            )
            logger.error("bundle main message outcome unknown", extra={"channel": channel})
            raise
        except Exception as exc:
            _record(
                publications,
                plan,
                decision_id=decision_id,
                status=PublicationStatus.FAILED,
                attempt_no=attempt_no,
                failure_reason=redact(str(exc)),
            )
            with transaction(connection):
                drafts.set_status(plan.draft.id, DraftStatus.PUBLISH_FAILED)
                drafts.set_status(plan.draft.id, DraftStatus.APPROVED)
            logger.error("bundle main message failed", extra={"channel": channel})
            raise

        published_at = now_utc()
        with transaction(connection):
            publication = _record(
                publications,
                plan,
                decision_id=decision_id,
                status=PublicationStatus.SUCCEEDED,
                attempt_no=attempt_no,
                message_id=outcome.message_id,
                chat_id=outcome.chat_id,
                published_at=published_at,
            )
            components.add(
                publication_id=publication.id,
                draft_id=plan.draft.id,
                draft_version_id=version.id,
                outcome=outcome,
            )

    # Everything else. execute() re-reads the component history, so the main message is
    # skipped rather than repeated, and a failure here leaves the post standing.
    try:
        execute(
            connection,
            client,
            bundle,
            version,
            publication_id=publication.id,
            draft_id=plan.draft.id,
            channel=channel,
            discussion_chat_id=plan.discussion_chat_id,
            media_root=root,
        )
    except (PublicationOutcomeUncertainError, Exception) as exc:
        # The post itself is out and recorded. A missing image or comment is a partial
        # publication, not a failed one, and re-sending the post to fix it would be the
        # one unrecoverable mistake available here.
        logger.error(
            "bundle partially published",
            extra={
                "draft_id": str(plan.draft.id),
                "channel": channel,
                "error": type(exc).__name__,
            },
        )
        with transaction(connection):
            if drafts.get(plan.draft.id).status is DraftStatus.PUBLISHING:
                drafts.set_status(plan.draft.id, DraftStatus.PUBLISHED)
        raise

    with transaction(connection):
        if drafts.get(plan.draft.id).status is DraftStatus.PUBLISHING:
            drafts.set_status(plan.draft.id, DraftStatus.PUBLISHED)

    logger.info(
        "bundle published",
        extra={
            "draft_id": str(plan.draft.id),
            "draft_version_id": str(version.id),
            "channel": channel,
            "message_id": publication.message_id,
        },
    )
    return publication
