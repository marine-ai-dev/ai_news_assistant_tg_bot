"""The human approval gate.

This module is the reason the project is shaped the way it is. Nothing reaches a channel
unless a human approved *the exact draft version being published*, and this is the only
place where that approval can be expressed.

Two functions carry the weight:

:func:`approve_draft` — the sole path from a pending draft to a valid
:class:`PublishAuthorization`. It reloads state from the database rather than trusting
whatever a caller was holding, records the human decision and flips the status in one
transaction, and only then mints the token.

:func:`verify_publication` — re-checks the whole chain immediately before a send. An
authorization is a claim about the past; this asks whether that claim still describes
the present.

Everything a caller might want to do instead — approve in bulk, approve by score,
approve without confirming — is absent on purpose. There is no flag for it because
there is no code for it.

Content policy lives next door in :mod:`publishing.eligibility` and is called from both
functions. Keeping it out of this module matters: the gate answers "did a human approve
this exact version?", and a gate that also carries editorial rules is a gate that
eventually grows an exception.
"""

from __future__ import annotations

import sqlite3
from uuid import UUID

from ai_news_editor.domain.authorization import (
    PublishAuthorization,
    issue_publication_authorization,
)
from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import DraftStatus, ReviewAction
from ai_news_editor.domain.errors import (
    ApprovalInvalidatedError,
    NotApprovedError,
)
from ai_news_editor.domain.models import Draft, DraftVersion, ReviewDecision
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.publishing.base import PublicationReceipt, Publisher
from ai_news_editor.publishing.eligibility import assert_publishable
from ai_news_editor.storage.db import transaction
from ai_news_editor.storage.repositories import DraftRepository, ReviewDecisionRepository

logger = get_logger(__name__)

#: Who approved, on a single-user local install. Not an authenticated identity and not
#: pretending to be one — a future Telegram review bot will supply a real account id.
DEFAULT_ACTOR = "owner"

MAX_NOTE_CHARS = 500


def approve_draft(
    connection: sqlite3.Connection,
    draft_id: UUID,
    *,
    actor: str = DEFAULT_ACTOR,
    note: str | None = None,
    expected_version_id: UUID | None = None,
) -> PublishAuthorization:
    """Record a human approval and issue the authorization it earns.

    Args:
        expected_version_id: the version the human was actually looking at. If the draft
            has moved on since it was displayed, approval fails rather than silently
            approving different text. A long review session must not approve whatever
            happens to be current when the operator finally presses a key.

    Raises:
        NotApprovedError: the draft is not awaiting review.
        ApprovalInvalidatedError: the draft changed since it was displayed, or has no
            content to approve.
    """
    drafts = DraftRepository(connection)
    decisions = ReviewDecisionRepository(connection)

    # Reload from storage. Whatever the caller was holding may be minutes old.
    draft = drafts.get(draft_id)
    if draft.status is not DraftStatus.PENDING_REVIEW:
        raise NotApprovedError(
            f"draft {draft_id} is {draft.status.value}; only a draft awaiting review can be "
            "approved"
        )
    if draft.current_version_id is None:
        raise ApprovalInvalidatedError(f"draft {draft_id} has no version to approve")

    version = drafts.current_version(draft_id)
    if expected_version_id is not None and version.id != expected_version_id:
        raise ApprovalInvalidatedError(
            f"draft {draft_id} moved to version {version.version_no} while it was being "
            "reviewed; re-read it before approving"
        )

    # Content policy, checked here so a human learns immediately rather than at the
    # publish prompt. Approving something that can never be published is a waste of the
    # only scarce resource in this pipeline.
    assert_publishable(connection, draft)

    decision = ReviewDecision(
        draft_id=draft.id,
        draft_version_id=version.id,
        # Snapshot of exactly what the human read. The authorization is bound to this.
        content_hash=version.content_hash,
        action=ReviewAction.APPROVE,
        actor=actor,
        note=_clean_note(note),
        created_at=now_utc(),
    )

    # One transaction: a stored approval with the draft still pending, or an approved
    # draft with no record of who approved it, are both corrupt states.
    with transaction(connection):
        decisions.add(decision)
        drafts.set_status(draft.id, DraftStatus.APPROVED)

    approved = drafts.get(draft.id)
    authorization = issue_publication_authorization(
        draft=approved, version=version, decision=decision
    )
    logger.info(
        "draft approved",
        extra={
            "draft_id": str(draft.id),
            "version_no": version.version_no,
            "actor": actor,
        },
    )
    return authorization


def authorization_for_approved_draft(
    connection: sqlite3.Connection, draft_id: UUID
) -> PublishAuthorization | None:
    """Rebuild the authorization for an already-approved draft, from storage alone.

    Approval and publication are separate human acts that may be separated by days, a
    restart, or a different process. An authorization is never serialized to survive
    that gap — a stored authorization would be a bearer token sitting in a file, which
    is the thing this gate exists to make impossible. Instead the *persisted authority*
    is reassembled and re-checked from scratch every time:

        the APPROVE row in review_decisions
        + the exact draft_versions row it names
        + the draft's current status and current version pointer

    In order, this requires:

    1. the draft still exists;
    2. its status is APPROVED — a later reject or rewrite changes it, so those
       invalidate the approval by construction;
    3. it has a current version;
    4. an APPROVE decision exists *for that exact version id* — an approval of an
       earlier version is not discoverable as an approval of this one;
    5. the decision's content hash still matches the version's computed hash;
    6. every check in :func:`issue_publication_authorization` passes.

    A later edit fails at (2) and (4) together: appending a version returns the draft to
    PENDING_REVIEW and the new version has no approval of its own.

    Returns ``None`` — not an exception — when no valid authorization exists. "This
    draft is not publishable" is an ordinary answer that callers ask for routinely.
    """
    drafts = DraftRepository(connection)
    decisions = ReviewDecisionRepository(connection)

    draft = drafts.get(draft_id)
    if draft.status is not DraftStatus.APPROVED or draft.current_version_id is None:
        return None

    version = drafts.current_version(draft_id)
    decision = decisions.latest_approval(draft.id, version.id)
    if decision is None:
        return None

    try:
        return issue_publication_authorization(
            draft=draft, version=version, decision=decision
        )
    except (NotApprovedError, ApprovalInvalidatedError):
        return None


def verify_publication(
    connection: sqlite3.Connection, authorization: PublishAuthorization
) -> tuple[Draft, DraftVersion]:
    """Re-check an authorization against live state, immediately before sending.

    An authorization records that something was true when it was issued. Between then
    and a send the draft may have been edited, rejected, or already published. Every
    link in the chain is checked again here.

    Raises:
        NotApprovedError, ApprovalInvalidatedError: the authorization no longer holds.
    """
    drafts = DraftRepository(connection)
    decisions = ReviewDecisionRepository(connection)

    draft = drafts.get(authorization.draft_id)
    if draft.status is not DraftStatus.APPROVED:
        raise NotApprovedError(
            f"draft {draft.id} is {draft.status.value}, not APPROVED; the authorization "
            "issued earlier no longer applies"
        )
    if draft.current_version_id != authorization.draft_version_id:
        raise ApprovalInvalidatedError(
            f"draft {draft.id} has moved to a different version since approval; "
            "the approved version is no longer current"
        )

    # The three checks below cannot fail while the append-only triggers on
    # draft_versions and review_decisions hold: the version id already matched above, so
    # a differing hash would mean a stored version was rewritten, and an authorization is
    # only ever issued against an APPROVE decision for that exact version. They stay
    # anyway. If those triggers are ever dropped, this is where it must still be caught,
    # and the cost of keeping them is three unreachable branches.
    # Again, immediately before a send. Approval and publication are separate acts and
    # policy has to hold at both — an approval recorded before a rule existed must not
    # carry content past it afterwards.
    assert_publishable(connection, draft)

    version = drafts.current_version(draft.id)
    if not authorization.authorizes(version):  # pragma: no cover - defensive
        raise ApprovalInvalidatedError(
            f"the authorization does not cover version {version.id}; its content changed "
            "after approval"
        )

    decision = decisions.get(authorization.decision_id)
    if decision.action is not ReviewAction.APPROVE:  # pragma: no cover - defensive
        raise NotApprovedError(
            f"review decision {decision.id} is {decision.action.value}, not an approval"
        )
    if (  # pragma: no cover - defensive
        decision.draft_version_id != version.id
        or decision.content_hash != version.content_hash
    ):
        raise ApprovalInvalidatedError(
            "the recorded approval refers to different content than the current version"
        )
    return draft, version


def publish_with_gate(
    connection: sqlite3.Connection,
    publisher: Publisher,
    authorization: PublishAuthorization,
) -> PublicationReceipt:
    """Verify an authorization, then hand the version to a publisher.

    Every publisher goes through here. A future Telegram implementation gets the same
    check as a local test double, because the check lives on this side of the boundary
    rather than inside whichever publisher happens to be running.
    """
    _draft, version = verify_publication(connection, authorization)
    logger.info(
        "publication gate passed",
        extra={
            "draft_id": str(authorization.draft_id),
            "version_no": authorization.version_no,
            "publisher": getattr(publisher, "name", type(publisher).__name__),
        },
    )
    return publisher.publish(version, authorization)


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    trimmed = " ".join(note.split())
    if not trimmed:
        return None
    return trimmed[:MAX_NOTE_CHARS]
