"""Approval-gate invariants. These tests must never be skipped or marked xfail.

The product rule: nothing reaches Telegram unless a human explicitly approved the exact
draft version being published. No Telegram client exists yet, and none is needed — the
property is a property of the domain, and it is proved here against real persisted state.

Each test class maps to one of the five required invariants:

1. an unapproved draft has no publication authorization
2. an approval applies only to the exact approved version and content
3. editing approved content invalidates the approval
4. a rejected draft cannot be treated as approved
5. no shortcut exists that mints an authorization from arbitrary unapproved content
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from ai_news_editor.domain.authorization import (
    PublishAuthorization,
    issue_publication_authorization,
)
from ai_news_editor.domain.enums import DraftStatus, ReviewAction
from ai_news_editor.domain.errors import (
    ApprovalInvalidatedError,
    NotApprovedError,
    UnauthorizedConstructionError,
)
from ai_news_editor.domain.models import Article, Draft, DraftVersion, ReviewDecision
from ai_news_editor.domain.transitions import DRAFT_TRANSITIONS, NON_PUBLISHABLE_DRAFT_STATES
from ai_news_editor.storage.repositories import DraftRepository, ReviewDecisionRepository
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety


def _approve(
    drafts: DraftRepository,
    decisions: ReviewDecisionRepository,
    draft: Draft,
    version: DraftVersion,
    *,
    actor: str = "marina",
) -> tuple[Draft, ReviewDecision]:
    """Walk a draft through review to APPROVED, as the Phase 6 CLI eventually will."""
    if drafts.get(draft.id).status is not DraftStatus.PENDING_REVIEW:
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    decision = decisions.add(
        ReviewDecision(
            draft_id=draft.id,
            draft_version_id=version.id,
            content_hash=version.content_hash,
            action=ReviewAction.APPROVE,
            actor=actor,
        )
    )
    return drafts.set_status(draft.id, DraftStatus.APPROVED), decision


@pytest.fixture
def draft_and_version(
    seeded_article: Article, drafts: DraftRepository
) -> tuple[Draft, DraftVersion]:
    return drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]


class TestInvariant1UnapprovedHasNoAuthorization:
    """An unapproved draft has no publication authorization."""

    def test_fresh_draft_cannot_be_authorized(
        self, draft_and_version: tuple[Draft, DraftVersion]
    ) -> None:
        draft, version = draft_and_version
        decision = ReviewDecision(
            draft_id=draft.id,
            draft_version_id=version.id,
            content_hash=version.content_hash,
            action=ReviewAction.APPROVE,
            actor="marina",
        )
        with pytest.raises(NotApprovedError, match="only APPROVED drafts"):
            issue_publication_authorization(draft=draft, version=version, decision=decision)

    @pytest.mark.parametrize("status", sorted(NON_PUBLISHABLE_DRAFT_STATES))
    def test_no_status_other_than_approved_can_be_authorized(
        self, draft_and_version: tuple[Draft, DraftVersion], status: DraftStatus
    ) -> None:
        """Swept over the whole enum: adding a status later cannot open a hole."""
        draft, version = draft_and_version
        unapproved = draft.model_copy(update={"status": status})
        decision = ReviewDecision(
            draft_id=draft.id,
            draft_version_id=version.id,
            content_hash=version.content_hash,
            action=ReviewAction.APPROVE,
            actor="marina",
        )
        with pytest.raises(NotApprovedError):
            issue_publication_authorization(draft=unapproved, version=version, decision=decision)

    @pytest.mark.parametrize(
        "action",
        [a for a in ReviewAction if a is not ReviewAction.APPROVE],
    )
    def test_non_approval_decisions_never_authorize(
        self, draft_and_version: tuple[Draft, DraftVersion], action: ReviewAction
    ) -> None:
        draft, version = draft_and_version
        approved = draft.model_copy(update={"status": DraftStatus.APPROVED})
        decision = ReviewDecision(
            draft_id=draft.id,
            draft_version_id=version.id,
            content_hash=version.content_hash,
            action=action,
            actor="marina",
        )
        with pytest.raises(NotApprovedError, match="not APPROVE"):
            issue_publication_authorization(draft=approved, version=version, decision=decision)

    def test_publishing_requires_passing_through_approved(self) -> None:
        """Structural check: PUBLISHING has exactly one predecessor."""
        predecessors = {
            status for status, targets in DRAFT_TRANSITIONS.items()
            if DraftStatus.PUBLISHING in targets
        }
        assert predecessors == {DraftStatus.APPROVED}


class TestInvariant2ApprovalIsExact:
    """An approval applies only to the exact approved draft version and content."""

    def test_approved_current_version_is_authorized(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version)

        auth = issue_publication_authorization(
            draft=approved, version=version, decision=decision
        )
        assert auth.draft_version_id == version.id
        assert auth.content_hash == version.content_hash
        assert auth.approved_by == "marina"
        assert auth.authorizes(version)

    def test_authorization_does_not_cover_a_different_version(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version)
        auth = issue_publication_authorization(
            draft=approved, version=version, decision=decision
        )

        _, other = drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Rewritten"})  # type: ignore[arg-type]
        assert auth.authorizes(other) is False

    def test_authorization_does_not_cover_identical_text_under_a_new_id(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        """Same words, different version: the human approved a specific version, not a string."""
        draft, version = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version)
        auth = issue_publication_authorization(
            draft=approved, version=version, decision=decision
        )

        twin = version.model_copy(update={"id": uuid4()})
        assert twin.content_hash == version.content_hash
        assert auth.authorizes(twin) is False

    def test_decision_belonging_to_another_draft_is_refused(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        approved, _ = _approve(drafts, decisions, draft, version)

        foreign = ReviewDecision(
            draft_id=uuid4(),
            draft_version_id=uuid4(),
            content_hash=version.content_hash,
            action=ReviewAction.APPROVE,
            actor="someone",
        )
        with pytest.raises(ApprovalInvalidatedError, match="does not refer to"):
            issue_publication_authorization(draft=approved, version=version, decision=foreign)

    def test_version_from_another_draft_is_refused(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version)
        stranger = version.model_copy(update={"draft_id": uuid4()})
        with pytest.raises(ApprovalInvalidatedError, match="does not belong"):
            issue_publication_authorization(
                draft=approved, version=stranger, decision=decision
            )

    def test_lookup_of_an_approval_is_scoped_to_the_version(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = draft_and_version
        _approve(drafts, decisions, draft, version)
        _, newer = drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Newer"})  # type: ignore[arg-type]
        assert decisions.latest_approval(draft.id, newer.id) is None


class TestInvariant3EditingInvalidatesApproval:
    """Editing approved content invalidates the approval and forces re-review."""

    def test_editing_moves_the_draft_out_of_approved(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = draft_and_version
        approved, _ = _approve(drafts, decisions, draft, version)
        assert approved.status is DraftStatus.APPROVED

        edited, _ = drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Edited text"})  # type: ignore[arg-type]
        assert edited.status is DraftStatus.PENDING_REVIEW

    def test_old_authorization_stops_covering_the_current_content(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        """The scenario from the brief: approve A, edit to B, A must not authorize B."""
        draft, version_a = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version_a)
        auth = issue_publication_authorization(
            draft=approved, version=version_a, decision=decision
        )
        assert auth.authorizes(version_a)

        drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Edited text"})  # type: ignore[arg-type]
        version_b = drafts.current_version(draft.id)

        assert version_b.content_hash != version_a.content_hash
        assert auth.authorizes(version_b) is False

    def test_stale_approval_cannot_be_reissued_for_the_new_version(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version_a = draft_and_version
        _, decision = _approve(drafts, decisions, draft, version_a)
        drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Edited text"})  # type: ignore[arg-type]

        # Force the draft back to APPROVED to simulate the worst case: a caller that
        # sets the status directly and tries to reuse the old approval record.
        forced = drafts.get(draft.id).model_copy(update={"status": DraftStatus.APPROVED})
        with pytest.raises(ApprovalInvalidatedError, match="no longer applies"):
            issue_publication_authorization(
                draft=forced, version=version_a, decision=decision
            )

    def test_content_change_alone_invalidates_even_with_matching_ids(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        """Tampering that keeps every id intact but changes the text is still caught."""
        draft, version = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version)
        tampered = version.model_copy(update={"body": "Silently swapped text"})
        assert tampered.id == version.id

        with pytest.raises(ApprovalInvalidatedError, match="changed after approval"):
            issue_publication_authorization(
                draft=approved, version=tampered, decision=decision
            )

    def test_re_approval_of_the_new_version_works(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        """The gate blocks stale approvals without blocking legitimate re-approval."""
        draft, version_a = draft_and_version
        _approve(drafts, decisions, draft, version_a)
        drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Edited text"})  # type: ignore[arg-type]

        version_b = drafts.current_version(draft.id)
        approved_b, decision_b = _approve(
            drafts, decisions, drafts.get(draft.id), version_b, actor="marina"
        )
        auth = issue_publication_authorization(
            draft=approved_b, version=version_b, decision=decision_b
        )
        assert auth.authorizes(version_b)
        assert auth.draft_version_id == version_b.id

    def test_every_version_remains_auditable(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version_a = draft_and_version
        _approve(drafts, decisions, draft, version_a)
        drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Edited text"})  # type: ignore[arg-type]

        history = decisions.list_for_draft(draft.id)
        assert [d.draft_version_id for d in history] == [version_a.id]
        assert len(drafts.list_versions(draft.id)) == 2


class TestInvariant4RejectedIsNeverApproved:
    """A rejected draft cannot be treated as approved."""

    def test_rejected_draft_cannot_be_authorized(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = draft_and_version
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        decision = decisions.add(
            ReviewDecision(
                draft_id=draft.id,
                draft_version_id=version.id,
                content_hash=version.content_hash,
                action=ReviewAction.REJECT,
                actor="marina",
                note="not interesting enough",
            )
        )
        rejected = drafts.set_status(draft.id, DraftStatus.REJECTED)

        with pytest.raises(NotApprovedError):
            issue_publication_authorization(
                draft=rejected, version=version, decision=decision
            )

    def test_rejected_is_terminal_and_cannot_reach_publishing(
        self, draft_and_version: tuple[Draft, DraftVersion], drafts: DraftRepository
    ) -> None:
        draft, _ = draft_and_version
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        drafts.set_status(draft.id, DraftStatus.REJECTED)

        assert DRAFT_TRANSITIONS[DraftStatus.REJECTED] == frozenset()
        assert drafts.claim_for_publishing(draft.id) is False
        assert drafts.get(draft.id).status is DraftStatus.REJECTED

    def test_a_rejection_is_not_discoverable_as_an_approval(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = draft_and_version
        decisions.add(
            ReviewDecision(
                draft_id=draft.id,
                draft_version_id=version.id,
                content_hash=version.content_hash,
                action=ReviewAction.REJECT,
                actor="marina",
            )
        )
        assert decisions.latest_approval(draft.id, version.id) is None


class TestInvariant5NoShortcutExists:
    """No shortcut mints an authorization from arbitrary unapproved content."""

    def test_direct_construction_is_refused(self) -> None:
        from ai_news_editor.domain.clock import now_utc

        with pytest.raises(UnauthorizedConstructionError, match="may only be created by"):
            PublishAuthorization(
                draft_id=uuid4(),
                draft_version_id=uuid4(),
                version_no=1,
                content_hash="deadbeef",
                approved_by="attacker",
                approved_at=now_utc(),
                decision_id=uuid4(),
            )

    def test_copying_a_real_token_with_new_fields_is_refused(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        """dataclasses.replace goes through __init__, so it is blocked too."""
        draft, version = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version)
        auth = issue_publication_authorization(
            draft=approved, version=version, decision=decision
        )

        with pytest.raises(UnauthorizedConstructionError):
            dataclasses.replace(auth, content_hash="something-else")

    def test_a_real_token_is_frozen(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version)
        auth = issue_publication_authorization(
            draft=approved, version=version, decision=decision
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            auth.content_hash = "tampered"  # type: ignore[misc]

    def test_the_issuing_flag_is_not_left_open_after_success(
        self,
        draft_and_version: tuple[Draft, DraftVersion],
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        """A leaked issuing window would let any later construction succeed."""
        draft, version = draft_and_version
        approved, decision = _approve(drafts, decisions, draft, version)
        issue_publication_authorization(draft=approved, version=version, decision=decision)

        with pytest.raises(UnauthorizedConstructionError):
            PublishAuthorization(
                draft_id=draft.id,
                draft_version_id=version.id,
                version_no=1,
                content_hash=version.content_hash,
                approved_by="attacker",
                approved_at=decision.created_at,
                decision_id=decision.id,
            )

    def test_the_issuing_flag_is_not_left_open_after_failure(
        self, draft_and_version: tuple[Draft, DraftVersion]
    ) -> None:
        draft, version = draft_and_version
        decision = ReviewDecision(
            draft_id=draft.id,
            draft_version_id=version.id,
            content_hash=version.content_hash,
            action=ReviewAction.REJECT,
            actor="marina",
        )
        with pytest.raises(NotApprovedError):
            issue_publication_authorization(draft=draft, version=version, decision=decision)

        from ai_news_editor.domain.clock import now_utc

        with pytest.raises(UnauthorizedConstructionError):
            PublishAuthorization(
                draft_id=uuid4(),
                draft_version_id=uuid4(),
                version_no=1,
                content_hash="x",
                approved_by="attacker",
                approved_at=now_utc(),
                decision_id=uuid4(),
            )

    def test_the_gate_is_the_only_producer(self) -> None:
        """Nothing else in the package constructs a PublishAuthorization."""
        from pathlib import Path

        import ai_news_editor

        package_root = Path(ai_news_editor.__file__).parent
        offenders = [
            path.relative_to(package_root)
            for path in package_root.rglob("*.py")
            if path.name != "authorization.py" and "PublishAuthorization(" in path.read_text()
        ]
        assert offenders == []


class TestNoIntegrationsExist:
    """Phase 1 has no outbound integrations; nothing can reach Telegram by accident."""

    def test_package_contains_no_publisher_or_network_client(self) -> None:
        from pathlib import Path

        import ai_news_editor

        package_root = Path(ai_news_editor.__file__).parent
        forbidden = ("import httpx", "import requests", "urllib.request", "api.telegram.org")
        offenders = [
            (path.relative_to(package_root).as_posix(), token)
            for path in package_root.rglob("*.py")
            for token in forbidden
            if token in path.read_text()
        ]
        assert offenders == []
