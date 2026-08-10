"""The human approval gate.

Nothing may reach Telegram unless a human explicitly approved *the exact draft version
being published*. Phase 1 establishes the domain mechanics of that rule; the publisher
that will consume them arrives in Phase 7.

The mechanism has three parts:

1. :class:`PublishAuthorization` can only be constructed from inside
   :func:`issue_publication_authorization`. Constructing one anywhere else raises.
   This is what stops a future code path from assembling a "valid-looking" token out
   of arbitrary unapproved content.
2. The issuer validates the whole chain — draft status, decision action, entity
   linkage, current-version pointer and content hash — and refuses on any mismatch.
3. The token names a content hash, and :meth:`PublishAuthorization.authorizes`
   recomputes that hash from the version it is offered. Editing a draft produces a new
   version with a different hash, so an old token stops matching.

Scope note: (1) blocks normal construction, ``dataclasses.replace`` and subclassing.
It is a guard against mistakes and refactors, not against a determined attacker with
``object.__new__``. The load-bearing guarantees against publishing wrong content are
(2) and (3), which are checked against real persisted state every time.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import final
from uuid import UUID

from ai_news_editor.domain.enums import DraftStatus, ReviewAction
from ai_news_editor.domain.errors import (
    ApprovalInvalidatedError,
    NotApprovedError,
    UnauthorizedConstructionError,
)
from ai_news_editor.domain.models import Draft, DraftVersion, ReviewDecision

_ISSUER_SENTINEL = object()
_issuing: ContextVar[object | None] = ContextVar("_issuing", default=None)


@final
@dataclass(frozen=True, slots=True)
class PublishAuthorization:
    """Proof that a named human approved one specific draft version.

    Obtainable only via :func:`issue_publication_authorization`. The future publisher
    takes this as a required argument, so "publish without approval" is not an error
    condition to remember to check — it is a call you cannot write.
    """

    draft_id: UUID
    draft_version_id: UUID
    version_no: int
    content_hash: str
    approved_by: str
    approved_at: datetime
    decision_id: UUID

    def __post_init__(self) -> None:
        if _issuing.get() is not _ISSUER_SENTINEL:
            raise UnauthorizedConstructionError(
                "PublishAuthorization may only be created by "
                "ai_news_editor.domain.authorization.issue_publication_authorization()"
            )

    def authorizes(self, version: DraftVersion) -> bool:
        """Return whether this token still covers ``version``.

        False as soon as the content differs, because ``version.content_hash`` is
        recomputed from the text rather than read from storage.
        """
        return version.id == self.draft_version_id and version.content_hash == self.content_hash


def issue_publication_authorization(
    *,
    draft: Draft,
    version: DraftVersion,
    decision: ReviewDecision,
) -> PublishAuthorization:
    """Validate an approval chain and mint the corresponding authorization.

    Raises:
        NotApprovedError: the draft is not approved, or the decision was not an approval.
        ApprovalInvalidatedError: the approval refers to content that is no longer current.
    """
    if decision.action is not ReviewAction.APPROVE:
        raise NotApprovedError(
            f"review decision {decision.id} is {decision.action.value}, not APPROVE"
        )
    if draft.status is not DraftStatus.APPROVED:
        raise NotApprovedError(
            f"draft {draft.id} is {draft.status.value}; only APPROVED drafts may be published"
        )
    if version.draft_id != draft.id:
        raise ApprovalInvalidatedError(f"version {version.id} does not belong to draft {draft.id}")
    if decision.draft_id != draft.id or decision.draft_version_id != version.id:
        raise ApprovalInvalidatedError(
            f"review decision {decision.id} does not refer to draft {draft.id} version {version.id}"
        )
    if draft.current_version_id != version.id:
        raise ApprovalInvalidatedError(
            f"draft {draft.id} has moved on to version {draft.current_version_id}; "
            f"the approval of version {version.id} no longer applies"
        )
    if decision.content_hash != version.content_hash:
        raise ApprovalInvalidatedError(
            f"content of version {version.id} changed after approval; re-approval is required"
        )

    token = _issuing.set(_ISSUER_SENTINEL)
    try:
        return PublishAuthorization(
            draft_id=draft.id,
            draft_version_id=version.id,
            version_no=version.version_no,
            content_hash=version.content_hash,
            approved_by=decision.actor,
            approved_at=decision.created_at,
            decision_id=decision.id,
        )
    finally:
        _issuing.reset(token)
