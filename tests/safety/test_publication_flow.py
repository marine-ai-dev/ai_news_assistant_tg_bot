"""No approval, no Telegram request. Never skip or xfail these.

Phase 6 proved a `PublishAuthorization` cannot be minted without a human. This proves
the consequence: a draft that lacks one cannot cause an HTTP request to leave the
machine. Every test here counts requests, and for anything unapproved the expected
count is zero — not "an error was raised", not "it failed safely". Zero.

The counting is done by a MockTransport that records and never reaches the network.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ai_news_editor.domain.enums import DraftStatus, PublicationStatus
from ai_news_editor.domain.errors import (
    ApprovalInvalidatedError,
    NotApprovedError,
    PublicationAlreadyExistsError,
    PublicationOutcomeUncertainError,
    TelegramError,
)
from ai_news_editor.publishing.gate import approve_draft, authorization_for_approved_draft
from ai_news_editor.publishing.service import (
    PublicationPlan,
    prepare_publication,
    publish_draft,
)
from ai_news_editor.publishing.telegram import TelegramClient, TelegramPublisher
from ai_news_editor.review.service import apply_edit, reject_draft, request_rewrite
from ai_news_editor.storage.repositories import DraftRepository, PublicationRepository
from ai_news_editor.writing.format import render_version
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety

CHANNEL = "@test_channel"
TOKEN = "123456789:" + "A" * 35

EDITED_BODY = (
    "Оновлений текст допису після редагування людиною. Він достатньо довгий, щоб "
    "пройти перевірку мінімальної довжини, і не містить жодної забороненої розмітки."
)


class RecordingTransport(httpx.MockTransport):
    """Counts every outbound request and answers without a network.

    ``sent`` is the assertion surface for the whole module: if a test expects nothing to
    reach Telegram, it asserts this list is empty.
    """

    def __init__(self, responder: Any = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.urls.append(str(request.url))
            self.sent.append(json.loads(request.content) if request.content else {})
            if responder is not None:
                return responder(request)
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 4242, "chat": {"id": -100777}}}
            )

        super().__init__(handler)


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def publisher(transport: RecordingTransport):  # type: ignore[no-untyped-def]
    with TelegramClient(TOKEN, transport=transport) as client:
        yield TelegramPublisher(client, CHANNEL)


@pytest.fixture
def pending(seeded_article, drafts: DraftRepository):  # type: ignore[no-untyped-def]
    draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return drafts.get(draft.id), version


def publish(connection: sqlite3.Connection, draft_id, publisher):  # type: ignore[no-untyped-def]
    plan = prepare_publication(connection, draft_id, channel=CHANNEL)
    return publish_draft(connection, plan, publisher)


class TestNothingUnapprovedReachesTelegram:
    """Zero requests. Not a handled error — no request at all."""

    def test_a_pending_draft_sends_nothing(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, _ = pending
        with pytest.raises(NotApprovedError):
            publish(connection, draft.id, publisher)
        assert transport.sent == []

    def test_a_rejected_draft_sends_nothing(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, _ = pending
        reject_draft(connection, draft.id, note="не для нас")
        with pytest.raises(NotApprovedError):
            publish(connection, draft.id, publisher)
        assert transport.sent == []

    def test_a_draft_needing_rewrite_sends_nothing(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, _ = pending
        request_rewrite(connection, draft.id, note="переписати")
        with pytest.raises(NotApprovedError):
            publish(connection, draft.id, publisher)
        assert transport.sent == []

    @pytest.mark.parametrize(
        "status",
        [s for s in DraftStatus if s is not DraftStatus.APPROVED],
    )
    def test_no_status_other_than_approved_can_publish(
        self,
        pending,
        connection: sqlite3.Connection,
        publisher,
        transport: RecordingTransport,
        status: DraftStatus,
        drafts: DraftRepository,
    ) -> None:
        """Over the whole enum, so a status added later is covered by construction."""
        draft, _ = pending
        connection.execute(
            "UPDATE drafts SET status = ? WHERE id = ?", (status.value, str(draft.id))
        )
        with pytest.raises((NotApprovedError, ApprovalInvalidatedError)):
            publish(connection, draft.id, publisher)
        assert transport.sent == []

    def test_an_approved_status_without_a_decision_sends_nothing(
        self, pending, connection: sqlite3.Connection, publisher,
        transport: RecordingTransport, drafts: DraftRepository,
    ) -> None:
        """Status is not evidence. The recorded human decision is."""
        draft, _ = pending
        drafts.set_status(draft.id, DraftStatus.APPROVED)
        with pytest.raises(NotApprovedError):
            publish(connection, draft.id, publisher)
        assert transport.sent == []


class TestEditAfterApproval:
    def test_editing_after_approval_sends_nothing(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)
        apply_edit(connection, draft.id, headline="🆕 Змінено", body=EDITED_BODY)

        with pytest.raises(NotApprovedError):
            publish(connection, draft.id, publisher)
        assert transport.sent == []

    def test_approving_the_new_version_sends_exactly_one_request_for_it(
        self, pending, connection: sqlite3.Connection, publisher,
        transport: RecordingTransport, drafts: DraftRepository,
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)
        apply_edit(connection, draft.id, headline="🆕 Змінено", body=EDITED_BODY)
        assert transport.sent == []

        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)

        assert len(transport.sent) == 1
        version_two = drafts.current_version(draft.id)
        assert transport.sent[0]["text"] == render_version(version_two)
        assert "🆕 Змінено" in transport.sent[0]["text"]

    def test_a_plan_prepared_before_an_edit_sends_nothing_after_it(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        """The window that matters: the preview is on screen while someone edits.

        A human reads the final preview, goes to make coffee, and the draft is edited
        from another terminal. The plan in hand is now stale, and typing PUBLISH must
        not send the version it was built from.
        """
        draft, _ = pending
        approve_draft(connection, draft.id)
        plan = prepare_publication(connection, draft.id, channel=CHANNEL)

        apply_edit(connection, draft.id, headline="🆕 Змінено", body=EDITED_BODY)

        with pytest.raises((NotApprovedError, ApprovalInvalidatedError)):
            publish_draft(connection, plan, publisher)
        assert transport.sent == []

    def test_a_stale_plan_sends_nothing_even_after_the_new_version_is_approved(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        """Approving V2 must not retroactively authorize a plan built for V1."""
        draft, _ = pending
        approve_draft(connection, draft.id)
        plan = prepare_publication(connection, draft.id, channel=CHANNEL)

        apply_edit(connection, draft.id, headline="🆕 Змінено", body=EDITED_BODY)
        approve_draft(connection, draft.id)

        with pytest.raises(ApprovalInvalidatedError):
            publish_draft(connection, plan, publisher)
        assert transport.sent == []


class TestRestartAuthorization:
    """Approval survives a restart; an authorization object does not have to."""

    def test_a_later_process_rebuilds_the_authorization_from_storage(
        self, pending, connection: sqlite3.Connection, tmp_path
    ) -> None:
        draft, version = pending
        approve_draft(connection, draft.id, actor="marina")

        # Process B: nothing carried over but the database itself.
        rebuilt = authorization_for_approved_draft(connection, draft.id)
        assert rebuilt is not None
        assert rebuilt.draft_version_id == version.id
        assert rebuilt.content_hash == version.content_hash
        assert rebuilt.approved_by == "marina"

    def test_the_rebuilt_authorization_publishes_the_exact_approved_version(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, version = pending
        approve_draft(connection, draft.id)

        publication = publish(connection, draft.id, publisher)
        assert publication.draft_version_id == version.id
        assert transport.sent[0]["text"] == render_version(version)

    def test_after_an_edit_a_later_process_gets_no_authorization(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        """approve V1 → edit to V2 → restart → V2 is not publishable."""
        draft, _ = pending
        approve_draft(connection, draft.id)
        apply_edit(connection, draft.id, headline="🆕 Друга версія", body=EDITED_BODY)

        assert authorization_for_approved_draft(connection, draft.id) is None

    def test_after_approving_v2_a_later_process_gets_an_authorization_for_v2(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, version_one = pending
        approve_draft(connection, draft.id)
        apply_edit(connection, draft.id, headline="🆕 Друга версія", body=EDITED_BODY)
        approve_draft(connection, draft.id)

        rebuilt = authorization_for_approved_draft(connection, draft.id)
        assert rebuilt is not None
        assert rebuilt.draft_version_id != version_one.id
        assert rebuilt.draft_version_id == drafts.current_version(draft.id).id

    def test_nothing_serializes_an_authorization(self) -> None:
        """The persisted authority is the decision plus the version, never a token."""
        from pathlib import Path

        import ai_news_editor

        package = Path(ai_news_editor.__file__).parent
        offenders = []
        for path in package.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            body = "\n".join(
                line for line in text.splitlines() if not line.strip().startswith("#")
            )
            if "PublishAuthorization" in body and any(
                marker in body
                for marker in ("json.dump", "pickle", "INSERT INTO publications")
            ):
                offenders.append(path.name)
        assert offenders == [], f"an authorization must never be persisted: {offenders}"

    def test_no_table_stores_an_authorization(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(publications)")
        }
        assert "authorization" not in columns
        assert not any("token" in name for name in columns)


class TestContentExactness:
    def test_telegram_receives_exactly_the_approved_text(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, version = pending
        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)

        assert transport.sent[0]["text"] == render_version(version)

    def test_the_destination_is_the_configured_channel(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)
        assert transport.sent[0]["chat_id"] == CHANNEL

    def test_no_extra_content_is_appended(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        """No signature, no promo line, no 'via'. What was approved, and nothing else."""
        draft, version = pending
        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)

        sent = transport.sent[0]["text"]
        assert len(sent) == len(render_version(version))

    def test_only_sendmessage_is_called(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)
        assert [url.rsplit("/", 1)[-1] for url in transport.urls] == ["sendMessage"]


class TestIdempotency:
    def test_a_second_publish_sends_nothing_and_says_already_published(
        self, pending, connection: sqlite3.Connection, publisher, transport: RecordingTransport
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)
        assert len(transport.sent) == 1

        # A second run: the draft is PUBLISHED, which is not publishable.
        with pytest.raises((NotApprovedError, PublicationAlreadyExistsError)):
            publish(connection, draft.id, publisher)
        assert len(transport.sent) == 1, "the channel must not receive a duplicate"

    def test_the_success_is_persisted_before_a_second_attempt_is_possible(
        self, pending, connection: sqlite3.Connection, publisher
    ) -> None:
        draft, version = pending
        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)

        stored = PublicationRepository(connection).successful_for_version(version.id, CHANNEL)
        assert stored is not None
        assert stored.message_id == 4242
        assert stored.status is PublicationStatus.SUCCEEDED

    def test_the_database_refuses_a_duplicate_success_row(
        self, pending, connection: sqlite3.Connection, publisher
    ) -> None:
        """Belt and braces: even a direct write cannot record two successes."""
        draft, version = pending
        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)

        stored = PublicationRepository(connection).successful_for_version(version.id, CHANNEL)
        assert stored is not None
        from ai_news_editor.domain.models import Publication

        with pytest.raises(PublicationAlreadyExistsError):
            PublicationRepository(connection).add(
                Publication(
                    draft_id=stored.draft_id,
                    draft_version_id=stored.draft_version_id,
                    review_decision_id=stored.review_decision_id,
                    content_hash=stored.content_hash,
                    channel=CHANNEL,
                    status=PublicationStatus.SUCCEEDED,
                    message_id=999,
                    published_at=stored.published_at,
                )
            )

    def test_the_draft_ends_published(
        self, pending, connection: sqlite3.Connection, publisher, drafts: DraftRepository
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)
        publish(connection, draft.id, publisher)
        assert drafts.get(draft.id).status is DraftStatus.PUBLISHED


class TestFailureHandling:
    def test_a_definite_failure_records_an_attempt_and_returns_the_draft_to_approved(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, _version = pending
        approve_draft(connection, draft.id)

        def refuse(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "Bad Request: nope"}
            )

        transport = RecordingTransport(refuse)
        with (
            TelegramClient(TOKEN, transport=transport) as client,
            pytest.raises(TelegramError),
        ):
            publish(connection, draft.id, TelegramPublisher(client, CHANNEL))

        assert drafts.get(draft.id).status is DraftStatus.APPROVED, "a retry must not need "
        attempts = PublicationRepository(connection).list_for_draft(draft.id)
        assert [a.status for a in attempts] == [PublicationStatus.FAILED]

    def test_a_failed_send_can_be_retried_without_a_second_approval(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)

        calls: list[int] = []

        def flaky(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(
                    500, json={"ok": False, "error_code": 500, "description": "server error"}
                )
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": -1}}}
            )

        transport = RecordingTransport(flaky)
        with TelegramClient(TOKEN, transport=transport) as client:
            publisher = TelegramPublisher(client, CHANNEL)
            with pytest.raises(TelegramError):
                publish(connection, draft.id, publisher)
            publication = publish(connection, draft.id, publisher)

        assert publication.message_id == 7
        assert publication.attempt_no == 2
        assert drafts.get(draft.id).status is DraftStatus.PUBLISHED


class TestUncertainOutcome:
    """A send that may or may not have landed is the case worth designing for."""

    def test_a_lost_response_records_uncertainty_and_does_not_resend(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lost", request=request)

        transport = RecordingTransport(timeout)
        with (
            TelegramClient(TOKEN, transport=transport) as client,
            pytest.raises(PublicationOutcomeUncertainError),
        ):
            publish(connection, draft.id, TelegramPublisher(client, CHANNEL))

        assert len(transport.sent) == 1, "a possibly-delivered post must not be sent again"
        attempts = PublicationRepository(connection).list_for_draft(draft.id)
        assert [a.status for a in attempts] == [PublicationStatus.UNCERTAIN]

    def test_an_uncertain_draft_stays_out_of_the_publishable_state(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        """PUBLISHING, deliberately: a human must decide what actually happened."""
        draft, _ = pending
        approve_draft(connection, draft.id)

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lost", request=request)

        with (
            TelegramClient(TOKEN, transport=RecordingTransport(timeout)) as client,
            pytest.raises(PublicationOutcomeUncertainError),
        ):
            publish(connection, draft.id, TelegramPublisher(client, CHANNEL))

        assert drafts.get(draft.id).status is DraftStatus.PUBLISHING

    def test_a_later_run_refuses_to_send_over_an_unresolved_attempt(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("lost", request=request)

        with (
            TelegramClient(TOKEN, transport=RecordingTransport(timeout)) as client,
            pytest.raises(PublicationOutcomeUncertainError),
        ):
            publish(connection, draft.id, TelegramPublisher(client, CHANNEL))

        # A human puts the draft back to APPROVED, believing it failed. It must still
        # refuse, because the earlier attempt is on record as unresolved.
        connection.execute(
            "UPDATE drafts SET status = 'APPROVED' WHERE id = ?", (str(draft.id),)
        )
        transport = RecordingTransport()
        with (
            TelegramClient(TOKEN, transport=transport) as client,
            pytest.raises(PublicationOutcomeUncertainError, match="resolve"),
        ):
            publish(connection, draft.id, TelegramPublisher(client, CHANNEL))
        assert transport.sent == []


class TestDryRun:
    def test_a_dry_run_validates_and_builds_but_sends_nothing(
        self, pending, connection: sqlite3.Connection, transport: RecordingTransport
    ) -> None:
        draft, version = pending
        approve_draft(connection, draft.id)

        plan = prepare_publication(connection, draft.id, channel=CHANNEL)
        assert plan.message.approved_text == render_version(version)
        assert transport.sent == []

    def test_a_dry_run_of_an_unapproved_draft_fails_the_same_way(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        draft, _ = pending
        with pytest.raises(NotApprovedError):
            prepare_publication(connection, draft.id, channel=CHANNEL)

    def test_a_dry_run_records_no_publication(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)
        prepare_publication(connection, draft.id, channel=CHANNEL)
        assert PublicationRepository(connection).count() == 0

    def test_a_dry_run_leaves_the_draft_approved(
        self, pending, connection: sqlite3.Connection, drafts: DraftRepository
    ) -> None:
        draft, _ = pending
        approve_draft(connection, draft.id)
        prepare_publication(connection, draft.id, channel=CHANNEL)
        assert drafts.get(draft.id).status is DraftStatus.APPROVED


class TestPublisherCannotApprove:
    def test_the_publisher_cannot_mint_an_authorization(self) -> None:
        from pathlib import Path

        import ai_news_editor

        source = (
            Path(ai_news_editor.__file__).parent / "publishing" / "telegram.py"
        ).read_text(encoding="utf-8")
        assert "issue_publication_authorization" not in source
        assert "approve_draft" not in source
        assert "set_status" not in source

    def test_the_publisher_refuses_a_version_its_authorization_does_not_cover(
        self, pending, connection: sqlite3.Connection, publisher,
        transport: RecordingTransport, drafts: DraftRepository, seeded_article,
    ) -> None:
        draft, _ = pending
        authorization = approve_draft(connection, draft.id)

        _other_draft, other_version = drafts.create(
            article_id=seeded_article.id, **DRAFT_CONTENT  # type: ignore[arg-type]
        )
        with pytest.raises(ApprovalInvalidatedError):
            publisher.publish(other_version, authorization)
        assert transport.sent == []

    def test_a_forged_authorization_cannot_be_constructed(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        from ai_news_editor.domain.authorization import PublishAuthorization
        from ai_news_editor.domain.clock import now_utc
        from ai_news_editor.domain.errors import UnauthorizedConstructionError

        draft, version = pending
        with pytest.raises(UnauthorizedConstructionError):
            PublishAuthorization(
                draft_id=draft.id,
                draft_version_id=version.id,
                version_no=1,
                content_hash=version.content_hash,
                approved_by="attacker",
                approved_at=now_utc(),
                decision_id=uuid4(),
            )


class TestAlreadyPublishedPlan:
    def test_a_plan_that_already_succeeded_refuses_before_sending(
        self, pending, connection: sqlite3.Connection
    ) -> None:
        """A stale plan held across a successful publish must not send a second copy."""
        draft, _ = pending
        approve_draft(connection, draft.id)
        plan = prepare_publication(connection, draft.id, channel=CHANNEL)

        first = RecordingTransport()
        with TelegramClient(TOKEN, transport=first) as client:
            publish_draft(connection, plan, TelegramPublisher(client, CHANNEL))
        assert len(first.sent) == 1

        # The same plan again — it predates the success it does not know about, so
        # publish_draft has to re-read the record rather than trust the plan.
        stale = PublicationPlan(
            draft=plan.draft,
            version=plan.version,
            authorization=plan.authorization,
            message=plan.message,
            channel=CHANNEL,
            already_published=PublicationRepository(connection).successful_for_version(
                plan.version.id, CHANNEL
            ),
        )
        second = RecordingTransport()
        with (
            TelegramClient(TOKEN, transport=second) as client,
            pytest.raises(PublicationAlreadyExistsError, match="already published"),
        ):
            publish_draft(connection, stale, TelegramPublisher(client, CHANNEL))
        assert second.sent == []
