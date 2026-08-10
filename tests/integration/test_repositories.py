"""Repository round-trips, constraints, and lifecycle enforcement."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from ai_news_editor.domain.enums import (
    ArticleStatus,
    AudienceTier,
    Category,
    DraftStatus,
    FetchOutcome,
    PrefilterReason,
)
from ai_news_editor.domain.errors import (
    EntityNotFoundError,
    IllegalStateTransition,
    RepositoryError,
)
from ai_news_editor.domain.models import Article, ReviewDecision
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    RawItemRepository,
    ReviewDecisionRepository,
    SourceFetchStateRepository,
    SourceRepository,
)
from tests.conftest import DRAFT_CONTENT, make_article, make_raw_item, make_source


class TestSourceRepository:
    def test_round_trip_preserves_fields(self, sources: SourceRepository) -> None:
        stored = sources.upsert(make_source(config={"selector": ".post"}))
        loaded = sources.get(stored.id)
        assert loaded.name == stored.name
        assert loaded.kind == stored.kind
        assert loaded.trust_tier == stored.trust_tier
        assert loaded.config == {"selector": ".post"}

    def test_upsert_updates_instead_of_failing(self, sources: SourceRepository) -> None:
        sources.upsert(make_source(name="Original"))
        sources.upsert(make_source(name="Renamed"))
        assert sources.get("test_source").name == "Renamed"
        assert sources.count() == 1

    def test_missing_source_raises(self, sources: SourceRepository) -> None:
        with pytest.raises(EntityNotFoundError):
            sources.get("nope")

    def test_find_returns_none_for_missing(self, sources: SourceRepository) -> None:
        assert sources.find("nope") is None

    def test_enabled_only_filter(self, sources: SourceRepository) -> None:
        sources.upsert(make_source("on", enabled=True))
        sources.upsert(make_source("off", enabled=False))
        assert [s.id for s in sources.list_all(enabled_only=True)] == ["on"]

    def test_database_rejects_unknown_status_values(
        self, connection: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
                "VALUES ('x', 'X', 'CARRIER_PIGEON', 'https://example.invalid', "
                "'OFFICIAL', 'now', 'now')"
            )

    def test_database_mirrors_the_signal_only_invariant(
        self, connection: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO sources (id, name, kind, url, trust_tier, signal_only, "
                "created_at, updated_at) VALUES ('x', 'X', 'RSS', "
                "'https://example.invalid', 'COMMUNITY_SIGNAL', 0, 'now', 'now')"
            )


class TestRawItemRepository:
    def test_round_trip(self, sources: SourceRepository, raw_items: RawItemRepository) -> None:
        sources.upsert(make_source())
        item = raw_items.add(make_raw_item(payload_raw='{"a": 1}'))
        loaded = raw_items.get(item.id)
        assert loaded.payload_raw == '{"a": 1}'
        assert loaded.url_original == item.url_original

    def test_requires_an_existing_source(self, raw_items: RawItemRepository) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            raw_items.add(make_raw_item("ghost_source"))

    def test_duplicate_external_id_per_source_is_rejected(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        sources.upsert(make_source())
        raw_items.add(make_raw_item(external_id="same"))
        with pytest.raises(sqlite3.IntegrityError):
            raw_items.add(make_raw_item(external_id="same"))

    def test_same_external_id_across_sources_is_fine(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        sources.upsert(make_source("a"))
        sources.upsert(make_source("b"))
        raw_items.add(make_raw_item("a", external_id="same"))
        raw_items.add(make_raw_item("b", external_id="same"))
        assert raw_items.count() == 2

    def test_null_external_ids_do_not_collide(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        sources.upsert(make_source())
        raw_items.add(make_raw_item(external_id=None))
        raw_items.add(make_raw_item(external_id=None))
        assert raw_items.count() == 2

    def test_exists_external_id(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        sources.upsert(make_source())
        raw_items.add(make_raw_item(external_id="known"))
        assert raw_items.exists_external_id("test_source", "known")
        assert not raw_items.exists_external_id("test_source", "unknown")

    def test_table_is_append_only(
        self,
        sources: SourceRepository,
        raw_items: RawItemRepository,
        connection: sqlite3.Connection,
    ) -> None:
        sources.upsert(make_source())
        item = raw_items.add(make_raw_item())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE raw_items SET title_original = 'tampered' WHERE id = ?", (str(item.id),)
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM raw_items WHERE id = ?", (str(item.id),))


class TestArticleRepository:
    def test_round_trip(self, seeded_article: Article, articles: ArticleRepository) -> None:
        loaded = articles.get(seeded_article.id)
        assert loaded.title == seeded_article.title
        assert loaded.status is ArticleStatus.COLLECTED

    def test_one_article_per_raw_item(
        self,
        sources: SourceRepository,
        raw_items: RawItemRepository,
        articles: ArticleRepository,
    ) -> None:
        sources.upsert(make_source())
        item = raw_items.add(make_raw_item())
        articles.add(make_article(item.id))
        with pytest.raises(sqlite3.IntegrityError):
            articles.add(make_article(item.id))

    def test_requires_an_existing_raw_item(self, articles: ArticleRepository) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            articles.add(make_article(uuid4(), "test_source"))

    def test_status_change_is_validated(
        self, seeded_article: Article, articles: ArticleRepository
    ) -> None:
        updated = articles.set_status(seeded_article.id, ArticleStatus.NORMALIZED)
        assert updated.status is ArticleStatus.NORMALIZED

    def test_illegal_status_change_is_refused(
        self, seeded_article: Article, articles: ArticleRepository
    ) -> None:
        with pytest.raises(IllegalStateTransition):
            articles.set_status(seeded_article.id, ArticleStatus.SHORTLISTED)
        assert articles.get(seeded_article.id).status is ArticleStatus.COLLECTED

    def test_screening_records_the_rule_that_fired(
        self, seeded_article: Article, articles: ArticleRepository
    ) -> None:
        articles.set_status(seeded_article.id, ArticleStatus.NORMALIZED)
        updated = articles.set_status(
            seeded_article.id,
            ArticleStatus.SCREENED_OUT,
            filtered_by=PrefilterReason.BOILERPLATE.value,
        )
        assert updated.filtered_by is PrefilterReason.BOILERPLATE

    def test_marking_a_duplicate(
        self,
        sources: SourceRepository,
        raw_items: RawItemRepository,
        articles: ArticleRepository,
    ) -> None:
        sources.upsert(make_source())
        original = articles.add(make_article(raw_items.add(make_raw_item()).id))
        copy = articles.add(make_article(raw_items.add(make_raw_item()).id))
        articles.set_status(copy.id, ArticleStatus.NORMALIZED)

        updated = articles.mark_duplicate(copy.id, original.id)
        assert updated.status is ArticleStatus.DUPLICATE
        assert updated.duplicate_of_id == original.id

    def test_self_duplicate_is_refused(
        self, seeded_article: Article, articles: ArticleRepository
    ) -> None:
        with pytest.raises(ValueError, match="duplicate of itself"):
            articles.mark_duplicate(seeded_article.id, seeded_article.id)

    def test_count_by_status(
        self, seeded_article: Article, articles: ArticleRepository
    ) -> None:
        assert articles.count_by_status() == {"COLLECTED": 1}


class TestDraftRepository:
    def test_create_makes_a_draft_and_first_version(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        assert draft.status is DraftStatus.DRAFTED
        assert draft.current_version_id == version.id
        assert version.version_no == 1

    def test_requires_an_existing_article(self, drafts: DraftRepository) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            drafts.create(article_id=uuid4(), **DRAFT_CONTENT)  # type: ignore[arg-type]

    def test_versions_increment_and_are_all_retained(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, _ = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Second"})  # type: ignore[arg-type]
        drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Third"})  # type: ignore[arg-type]

        versions = drafts.list_versions(draft.id)
        assert [v.version_no for v in versions] == [1, 2, 3]
        assert [v.body for v in versions] == ["Placeholder body text.", "Second", "Third"]

    def test_current_version_follows_the_newest(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, first = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        _, second = drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "New"})  # type: ignore[arg-type]
        current = drafts.current_version(draft.id)
        assert current.id == second.id
        assert current.id != first.id

    def test_duplicate_version_number_is_rejected(
        self, seeded_article: Article, drafts: DraftRepository, connection: sqlite3.Connection
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO draft_versions (id, draft_id, version_no, title, body, category, "
                "audience, source_attribution, content_hash, created_by, created_at) "
                "VALUES (?, ?, 1, 't', 'b', 'WOW', 'GENERAL', 'a', 'h', 'test', 'now')",
                (str(uuid4()), str(draft.id)),
            )
        assert len(drafts.list_versions(draft.id)) == 1
        assert version.version_no == 1

    def test_versions_are_immutable_in_the_database(
        self, seeded_article: Article, drafts: DraftRepository, connection: sqlite3.Connection
    ) -> None:
        _, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE draft_versions SET body = 'tampered' WHERE id = ?", (str(version.id),)
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM draft_versions WHERE id = ?", (str(version.id),))

    def test_stored_hash_matches_the_recomputed_one(
        self, seeded_article: Article, drafts: DraftRepository, connection: sqlite3.Connection
    ) -> None:
        _, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        row = connection.execute(
            "SELECT content_hash FROM draft_versions WHERE id = ?", (str(version.id),)
        ).fetchone()
        assert row["content_hash"] == version.content_hash
        assert drafts.get_version(version.id).content_hash == version.content_hash

    def test_hashtags_survive_the_round_trip(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        _, version = drafts.create(
            article_id=seeded_article.id, hashtags=["#ШІ", "#новини"], **DRAFT_CONTENT  # type: ignore[arg-type]
        )
        assert drafts.get_version(version.id).hashtags == ("#ШІ", "#новини")

    @pytest.mark.parametrize(
        "status", [DraftStatus.PUBLISHING, DraftStatus.PUBLISHED, DraftStatus.REJECTED]
    )
    def test_cannot_append_to_terminal_or_inflight_drafts(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        connection: sqlite3.Connection,
        status: DraftStatus,
    ) -> None:
        draft, _ = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        connection.execute(
            "UPDATE drafts SET status = ? WHERE id = ?", (status.value, str(draft.id))
        )
        with pytest.raises(RepositoryError, match="cannot append"):
            drafts.append_version(draft.id, **DRAFT_CONTENT)  # type: ignore[arg-type]

    def test_rewrite_request_returns_to_drafted(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, _ = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        drafts.set_status(draft.id, DraftStatus.NEEDS_REWRITE)
        updated, _ = drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Rewritten"})  # type: ignore[arg-type]
        assert updated.status is DraftStatus.DRAFTED

    def test_illegal_status_change_is_refused(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, _ = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        with pytest.raises(IllegalStateTransition):
            drafts.set_status(draft.id, DraftStatus.PUBLISHED)

    def test_missing_draft_raises(self, drafts: DraftRepository) -> None:
        with pytest.raises(EntityNotFoundError):
            drafts.get(uuid4())


class TestClaimForPublishing:
    def _approved(self, article: Article, drafts: DraftRepository):  # type: ignore[no-untyped-def]
        draft, version = drafts.create(article_id=article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        drafts.set_status(draft.id, DraftStatus.APPROVED)
        return draft, version

    def test_approved_draft_is_claimed_once(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, _ = self._approved(seeded_article, drafts)
        assert drafts.claim_for_publishing(draft.id) is True
        assert drafts.claim_for_publishing(draft.id) is False
        assert drafts.get(draft.id).status is DraftStatus.PUBLISHING

    def test_unapproved_draft_cannot_be_claimed(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, _ = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        assert drafts.claim_for_publishing(draft.id) is False
        assert drafts.get(draft.id).status is DraftStatus.DRAFTED

    def test_unknown_draft_cannot_be_claimed(self, drafts: DraftRepository) -> None:
        assert drafts.claim_for_publishing(uuid4()) is False


class TestReviewDecisionRepository:
    def _decision(self, draft_id, version, **overrides):  # type: ignore[no-untyped-def]
        data = {
            "draft_id": draft_id,
            "draft_version_id": version.id,
            "content_hash": version.content_hash,
            "action": "APPROVE",
            "actor": "marina",
        }
        data.update(overrides)
        return ReviewDecision.model_validate(data)

    def test_round_trip(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        stored = decisions.add(self._decision(draft.id, version, note="looks good"))
        loaded = decisions.get(stored.id)
        assert loaded.actor == "marina"
        assert loaded.note == "looks good"
        assert loaded.content_hash == version.content_hash

    def test_requires_an_existing_version(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        phantom = version.model_copy(update={"id": uuid4()})
        with pytest.raises(sqlite3.IntegrityError):
            decisions.add(self._decision(draft.id, phantom))

    def test_table_is_append_only(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
        connection: sqlite3.Connection,
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        stored = decisions.add(self._decision(draft.id, version))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE review_decisions SET action = 'REJECT' WHERE id = ?", (str(stored.id),)
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM review_decisions WHERE id = ?", (str(stored.id),))

    def test_latest_approval_is_scoped_to_one_version(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, first = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        decisions.add(self._decision(draft.id, first))
        _, second = drafts.append_version(draft.id, **{**DRAFT_CONTENT, "body": "Edited"})  # type: ignore[arg-type]

        assert decisions.latest_approval(draft.id, first.id) is not None
        assert decisions.latest_approval(draft.id, second.id) is None

    def test_rejection_is_not_reported_as_approval(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        decisions.add(self._decision(draft.id, version, action="REJECT"))
        assert decisions.latest_approval(draft.id, version.id) is None

    def test_full_history_is_preserved_in_order(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        for action in ("SKIP", "REQUEST_REWRITE", "APPROVE"):
            decisions.add(self._decision(draft.id, version, action=action))
        assert [d.action.value for d in decisions.list_for_draft(draft.id)] == [
            "SKIP",
            "REQUEST_REWRITE",
            "APPROVE",
        ]


class TestListingHelpers:
    def test_raw_items_are_listed_per_source(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        sources.upsert(make_source("a"))
        sources.upsert(make_source("b"))
        raw_items.add(make_raw_item("a"))
        raw_items.add(make_raw_item("a"))
        raw_items.add(make_raw_item("b"))
        assert len(raw_items.list_by_source("a")) == 2
        assert len(raw_items.list_by_source("b")) == 1

    def test_articles_are_listed_by_status(
        self, seeded_article: Article, articles: ArticleRepository
    ) -> None:
        assert [a.id for a in articles.list_by_status(ArticleStatus.COLLECTED)] == [
            seeded_article.id
        ]
        assert articles.list_by_status(ArticleStatus.SHORTLISTED) == []

    def test_article_is_findable_by_raw_item(
        self, seeded_article: Article, articles: ArticleRepository
    ) -> None:
        found = articles.find_by_raw_item(seeded_article.raw_item_id)
        assert found is not None
        assert found.id == seeded_article.id
        assert articles.find_by_raw_item(uuid4()) is None

    def test_drafts_are_listed_and_counted_by_status(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, _ = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        assert [d.id for d in drafts.list_by_status(DraftStatus.DRAFTED)] == [draft.id]
        assert drafts.count_by_status() == {"DRAFTED": 1}

    def test_missing_version_raises(self, drafts: DraftRepository) -> None:
        with pytest.raises(EntityNotFoundError):
            drafts.get_version(uuid4())

    def test_current_version_of_a_versionless_draft_raises(
        self, seeded_article: Article, drafts: DraftRepository, connection: sqlite3.Connection
    ) -> None:
        draft, _ = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        connection.execute(
            "UPDATE drafts SET current_version_id = NULL WHERE id = ?", (str(draft.id),)
        )
        with pytest.raises(EntityNotFoundError, match="no versions"):
            drafts.current_version(draft.id)

    def test_missing_decision_raises_and_count_works(
        self,
        seeded_article: Article,
        drafts: DraftRepository,
        decisions: ReviewDecisionRepository,
    ) -> None:
        with pytest.raises(EntityNotFoundError):
            decisions.get(uuid4())
        assert decisions.count() == 0

        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        decisions.add(
            ReviewDecision(
                draft_id=draft.id,
                draft_version_id=version.id,
                content_hash=version.content_hash,
                action="SKIP",
                actor="marina",
            )
        )
        assert decisions.count() == 1


class TestProvenanceChain:
    def test_a_draft_traces_back_to_its_raw_payload(
        self,
        sources: SourceRepository,
        raw_items: RawItemRepository,
        articles: ArticleRepository,
        drafts: DraftRepository,
    ) -> None:
        """The trace a published post must always support: draft → article → raw → source."""
        source = sources.upsert(make_source())
        item = raw_items.add(make_raw_item(source.id, payload_raw='{"origin": "recorded"}'))
        article = articles.add(make_article(item.id, source.id))
        draft, version = drafts.create(article_id=article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]

        found_article = articles.get(drafts.get(draft.id).article_id)
        found_raw = raw_items.get(found_article.raw_item_id)
        found_source = sources.get(found_raw.source_id)

        assert version.draft_id == draft.id
        assert found_raw.payload_raw == '{"origin": "recorded"}'
        assert found_source.id == source.id


class TestDomainTypesNotRawRows:
    def test_repositories_return_domain_objects(
        self, seeded_article: Article, drafts: DraftRepository
    ) -> None:
        draft, version = drafts.create(article_id=seeded_article.id, **DRAFT_CONTENT)  # type: ignore[arg-type]
        assert not isinstance(draft, sqlite3.Row)
        assert isinstance(version.category, Category)
        assert isinstance(version.audience, AudienceTier)
        assert isinstance(drafts.get(draft.id).status, DraftStatus)


class TestSourceFetchStateRepository:
    @pytest.fixture(autouse=True)
    def _source(self, sources: SourceRepository) -> None:
        sources.upsert(make_source())

    def test_unknown_source_returns_a_blank_state(
        self, fetch_states: SourceFetchStateRepository
    ) -> None:
        """A never-fetched source has no state, which is not an error."""
        state = fetch_states.get("test_source")
        assert state.source_id == "test_source"
        assert state.etag is None
        assert state.last_success_at is None
        assert state.consecutive_failures == 0

    def test_find_distinguishes_absent_from_blank(
        self, fetch_states: SourceFetchStateRepository
    ) -> None:
        assert fetch_states.find("test_source") is None

    def test_success_stores_validators(self, fetch_states: SourceFetchStateRepository) -> None:
        fetch_states.record_success(
            "test_source",
            outcome=FetchOutcome.OK,
            etag='W/"v1"',
            last_modified="Mon, 03 Aug 2026 10:30:00 GMT",
            http_status=200,
        )
        state = fetch_states.get("test_source")
        assert state.etag == 'W/"v1"'
        assert state.last_modified == "Mon, 03 Aug 2026 10:30:00 GMT"
        assert state.last_outcome is FetchOutcome.OK
        assert state.last_success_at is not None
        assert state.last_success_at.tzinfo is not None

    def test_not_modified_counts_as_success(
        self, fetch_states: SourceFetchStateRepository
    ) -> None:
        fetch_states.record_success(
            "test_source",
            outcome=FetchOutcome.NOT_MODIFIED,
            etag=None,
            last_modified=None,
            http_status=304,
        )
        state = fetch_states.get("test_source")
        assert state.last_outcome is FetchOutcome.NOT_MODIFIED
        assert state.last_success_at is not None
        assert state.consecutive_failures == 0

    def test_failures_accumulate(self, fetch_states: SourceFetchStateRepository) -> None:
        for _ in range(3):
            fetch_states.record_failure("test_source", error="boom", http_status=500)
        state = fetch_states.get("test_source")
        assert state.consecutive_failures == 3
        assert state.last_error == "boom"
        assert state.last_http_status == 500

    def test_success_resets_the_failure_count(
        self, fetch_states: SourceFetchStateRepository
    ) -> None:
        fetch_states.record_failure("test_source", error="boom")
        fetch_states.record_success(
            "test_source", outcome=FetchOutcome.OK, etag=None, last_modified=None, http_status=200
        )
        state = fetch_states.get("test_source")
        assert state.consecutive_failures == 0
        assert state.last_error is None

    def test_a_long_error_is_truncated(self, fetch_states: SourceFetchStateRepository) -> None:
        fetch_states.record_failure("test_source", error="x" * 10_000)
        assert len(fetch_states.get("test_source").last_error or "") <= 2000

    def test_state_requires_an_existing_source(
        self, fetch_states: SourceFetchStateRepository
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            fetch_states.record_failure("ghost_source", error="boom")

    def test_list_all_returns_domain_objects(
        self, fetch_states: SourceFetchStateRepository
    ) -> None:
        fetch_states.record_failure("test_source", error="boom")
        states = fetch_states.list_all()
        assert len(states) == 1
        assert states[0].last_outcome is FetchOutcome.ERROR


class TestRawItemIdempotency:
    def test_add_if_absent_reports_new_then_existing(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        sources.upsert(make_source())
        item = make_raw_item(external_id="stable-1")
        assert raw_items.add_if_absent(item) is True
        assert raw_items.add_if_absent(make_raw_item(external_id="stable-1")) is False
        assert raw_items.count() == 1

    def test_different_external_ids_both_insert(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        sources.upsert(make_source())
        assert raw_items.add_if_absent(make_raw_item(external_id="a"))
        assert raw_items.add_if_absent(make_raw_item(external_id="b"))
        assert raw_items.count() == 2

    def test_the_same_external_id_across_sources_is_not_a_duplicate(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        """Identity is per source; cross-source overlap is editorial dedup, not ingestion."""
        sources.upsert(make_source("first"))
        sources.upsert(make_source("second"))
        assert raw_items.add_if_absent(make_raw_item("first", external_id="shared"))
        assert raw_items.add_if_absent(make_raw_item("second", external_id="shared"))
        assert raw_items.count() == 2

    def test_an_item_without_an_external_id_is_always_inserted(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        sources.upsert(make_source())
        assert raw_items.add_if_absent(make_raw_item(external_id=None))
        assert raw_items.add_if_absent(make_raw_item(external_id=None))
        assert raw_items.count() == 2

    def test_the_first_version_of_an_item_is_kept(
        self, sources: SourceRepository, raw_items: RawItemRepository
    ) -> None:
        """Conflicts do nothing rather than overwrite: raw provenance is append-only."""
        sources.upsert(make_source())
        raw_items.add_if_absent(make_raw_item(external_id="x", title_original="Original"))
        raw_items.add_if_absent(make_raw_item(external_id="x", title_original="Changed later"))
        stored = raw_items.list_by_source("test_source")
        assert [item.title_original for item in stored] == ["Original"]
