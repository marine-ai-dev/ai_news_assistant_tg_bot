"""A post is more than its text, so an approval must cover more than its text.

Phase 8.2 lets a post carry a comment, images and a downloadable resource. Each of those
is a way for what gets published to differ from what a human read — approve the post,
change the comment, send both. These tests exist to make that impossible.

The other half is backward compatibility. The channel already has a published post, and
its approval must keep verifying. A text-only version has to hash exactly as it did
before any of this existed.
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_news_editor.domain.content import canonical_bundle, compute_content_hash
from ai_news_editor.domain.enums import (
    AudienceTier,
    Category,
    ContentType,
    DraftStatus,
    MediaOrigin,
    MediaRole,
    PromptPlacement,
    ResourceType,
)
from ai_news_editor.domain.models import DraftVersion, MediaAsset, ResourceSpec
from ai_news_editor.publishing.gate import approve_draft, authorization_for_approved_draft
from ai_news_editor.storage.repositories import DraftRepository
from ai_news_editor.writing.footer import FOOTER_EMOJI, FooterError, check_footer, render_footer
from ai_news_editor.writing.format import render_version
from tests.conftest import DRAFT_CONTENT

pytestmark = pytest.mark.safety

HANDLE = "@learn_ai_easy"
COMMENT = "Повний промпт: опиши задачу, дай контекст і скажи, у якому вигляді хочеш відповідь."


def version(**overrides: object) -> DraftVersion:
    data: dict[str, object] = {
        "draft_id": uuid4(),
        "version_no": 1,
        "title": "🆕 Заголовок",
        "body": "Текст допису.",
        "category": Category.EVERYDAY_AI,
        "audience": AudienceTier.NEWCOMER,
        "source_attribution": "🔗 Джерело: X\nhttps://x.invalid",
        "created_by": "test",
    }
    data.update(overrides)
    return DraftVersion(**data)  # type: ignore[arg-type]


def image(**overrides: object) -> MediaAsset:
    data: dict[str, object] = {
        "role": MediaRole.RESULT_IMAGE,
        "origin": MediaOrigin.OWNER_GENERATED,
        "reference": "media/result-001.png",
        "description": "результат генерації",
        "tool_used": "Gemini",
    }
    data.update(overrides)
    return MediaAsset(**data)  # type: ignore[arg-type]


class TestBackwardCompatibility:
    """The already-published post's approval must keep verifying."""

    def test_a_text_only_version_hashes_as_it_always_did(self) -> None:
        """The pre-8.2 payload, computed by hand, must still match."""
        plain = version()
        legacy = compute_content_hash(
            title=plain.title,
            body=plain.body,
            hashtags=(),
            category=plain.category.value,
            audience=plain.audience.value,
            source_attribution=plain.source_attribution,
        )
        assert plain.content_hash == legacy

    def test_an_empty_bundle_contributes_nothing(self) -> None:
        assert version().bundle() == {}
        assert canonical_bundle({}) == {}
        assert canonical_bundle({"comment_text": "", "media": []}) == {}

    def test_the_stored_hashes_of_existing_versions_are_unchanged(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        """A round trip through storage must not alter a hash."""
        _draft, created = drafts.create(
            article_id=seeded_article.id, **DRAFT_CONTENT  # type: ignore[arg-type]
        )
        reloaded = drafts.current_version(created.draft_id)
        assert reloaded.content_hash == created.content_hash

    def test_adding_a_bundle_field_to_the_vocabulary_cannot_move_old_hashes(self) -> None:
        """Empty values are dropped, so a new key never disturbs content that lacks it."""
        base = {
            "title": "t", "body": "b", "hashtags": (), "category": "c",
            "audience": "a", "source_attribution": "s",
        }
        assert compute_content_hash(**base) == compute_content_hash(  # type: ignore[arg-type]
            **base, bundle={"a_field_added_later": None, "another": ()}  # type: ignore[arg-type]
        )


class TestTheApprovalCoversTheWholeBundle:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("comment_text", COMMENT),
            ("footer_text", "👉 Запросити друзів: @learn_ai_easy"),
        ],
    )
    def test_changing_a_bundle_field_changes_the_hash(
        self, field: str, value: object
    ) -> None:
        plain = version()
        changed = version(**{field: value})
        assert changed.content_hash != plain.content_hash

    def test_changing_the_comment_changes_the_hash(self) -> None:
        one = version(prompt_placement=PromptPlacement.COMMENT, comment_text=COMMENT)
        two = version(
            prompt_placement=PromptPlacement.COMMENT, comment_text=COMMENT + " Ще рядок."
        )
        assert one.content_hash != two.content_hash

    def test_changing_the_media_changes_the_hash(self) -> None:
        one = version(media=(image(),))
        two = version(media=(image(reference="media/result-002.png"),))
        assert one.content_hash != two.content_hash

    def test_changing_the_media_role_changes_the_hash(self) -> None:
        one = version(media=(image(),))
        two = version(media=(image(role=MediaRole.SCREENSHOT),))
        assert one.content_hash != two.content_hash

    def test_changing_the_resource_changes_the_hash(self) -> None:
        one = version(
            resource=ResourceSpec(
                resource_type=ResourceType.PDF_COLLECTION, title="A", description="d"
            )
        )
        two = version(
            resource=ResourceSpec(
                resource_type=ResourceType.PDF_COLLECTION, title="B", description="d"
            )
        )
        assert one.content_hash != two.content_hash

    def test_changing_the_footer_handle_changes_the_hash(self) -> None:
        """A publisher cannot swap the channel out from under an approval."""
        one = version(footer_text=f"👉 Запросити друзів: {HANDLE}")
        two = version(footer_text="👉 Запросити друзів: @somewhere_else")
        assert one.content_hash != two.content_hash

    def test_file_system_noise_is_not_hashed(self) -> None:
        """Touching a file must not expire an approval."""
        asset = image()
        assert set(asset.identity()) == {"role", "origin", "reference"}
        assert "generated_at" not in asset.identity()
        assert "model_version" not in asset.identity()


class TestBundleCoherence:
    def test_a_comment_placement_without_a_comment_is_refused(self) -> None:
        """The post would promise a prompt that is not there."""
        with pytest.raises(ValidationError, match="no comment text"):
            version(prompt_placement=PromptPlacement.COMMENT)

    def test_owner_generated_media_must_name_its_tool(self) -> None:
        with pytest.raises(ValidationError, match="not a provenance record"):
            image(tool_used=None)

    def test_source_media_must_name_the_page_it_belongs_to(self) -> None:
        """We point at somebody's image; we do not download and republish it."""
        with pytest.raises(ValidationError, match="page it belongs to"):
            image(origin=MediaOrigin.SOURCE_MEDIA, tool_used=None)

    def test_source_media_is_referenced_not_copied(self) -> None:
        asset = image(
            origin=MediaOrigin.SOURCE_MEDIA,
            tool_used=None,
            reference="https://example.invalid/pic.png",
            source_url="https://example.invalid/post",
        )
        assert asset.source_url


class TestFooter:
    def test_the_footer_names_the_configured_channel(self) -> None:
        assert HANDLE in render_footer(HANDLE)

    def test_the_leading_emoji_varies(self) -> None:
        """Four hundred identical closing lines is what a template looks like."""
        rendered = {render_footer(HANDLE)[0] for _ in range(60)}
        assert len(rendered) > 1
        assert rendered <= set(FOOTER_EMOJI)

    def test_a_footer_naming_another_channel_is_refused(self) -> None:
        with pytest.raises(FooterError, match="does not name"):
            check_footer("👉 Запросити друзів: @someone_else", HANDLE)

    def test_the_footer_is_separated_from_the_source_line(self) -> None:
        """One says where the story came from, the other where the reader is."""
        rendered = render_version(
            version(
                source_url="https://x.invalid",
                footer_text=render_footer(HANDLE, emoji="👉"),
            )
        )
        assert "🔗 Джерело" in rendered
        assert rendered.rstrip().endswith(HANDLE)
        assert "\n\n👉" in rendered

    def test_a_version_without_a_footer_renders_without_one(self) -> None:
        """Every post published before Phase 8.2 keeps rendering as it did."""
        assert HANDLE not in render_version(version(source_url="https://x.invalid"))

    def test_the_stored_footer_is_used_not_the_current_configuration(self) -> None:
        """Changing a setting must not alter a post a human already approved."""
        approved = version(footer_text="👉 Запросити друзів: @learn_ai_easy")
        assert "@learn_ai_easy" in render_version(approved)


class TestStaleBundleProtection:
    """Approve the post you read, not the one that replaced it."""

    def _bundled_draft(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ):  # type: ignore[no-untyped-def]
        content = {**DRAFT_CONTENT}
        draft, created = drafts.create(
            article_id=seeded_article.id,
            **content,  # type: ignore[arg-type]
            prompt_placement=PromptPlacement.COMMENT,
            comment_text=COMMENT,
            footer_text=render_footer(HANDLE, emoji="👉"),
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        return drafts.get(draft.id), created

    def test_the_comment_survives_storage(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        draft, created = self._bundled_draft(connection, seeded_article, drafts)
        reloaded = drafts.current_version(draft.id)

        assert reloaded.comment_text == COMMENT
        assert reloaded.prompt_placement is PromptPlacement.COMMENT
        assert reloaded.content_hash == created.content_hash

    def test_changing_the_comment_invalidates_an_approval(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        """Approve with comment V1, replace the comment, and the approval is gone."""
        draft, _ = self._bundled_draft(connection, seeded_article, drafts)
        approve_draft(connection, draft.id)
        assert authorization_for_approved_draft(connection, draft.id) is not None

        drafts.append_version(
            draft.id,
            **{k: v for k, v in DRAFT_CONTENT.items() if k != "post_format"},  # type: ignore[arg-type]
            prompt_placement=PromptPlacement.COMMENT,
            comment_text=COMMENT + " Змінено.",
            footer_text=render_footer(HANDLE, emoji="👉"),
        )

        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert authorization_for_approved_draft(connection, draft.id) is None

    def test_changing_the_media_invalidates_an_approval(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        draft, _created = drafts.create(
            article_id=seeded_article.id,
            **DRAFT_CONTENT,  # type: ignore[arg-type]
            media=(image(),),
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        approve_draft(connection, draft.id)

        drafts.append_version(
            draft.id,
            **{k: v for k, v in DRAFT_CONTENT.items() if k != "post_format"},  # type: ignore[arg-type]
            media=(image(reference="media/other.png"),),
        )

        assert authorization_for_approved_draft(connection, draft.id) is None

    def test_an_authorization_does_not_cover_a_differently_bundled_version(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        draft, approved_version = self._bundled_draft(connection, seeded_article, drafts)
        authorization = approve_draft(connection, draft.id)

        rebundled = approved_version.model_copy(
            update={"comment_text": COMMENT + " Інакше."}
        )
        assert not authorization.authorizes(rebundled)


class TestRegressions:
    def test_news_needs_no_bundle_at_all(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        """No compulsory media, comment, footer or theme."""
        draft, created = drafts.create(
            article_id=seeded_article.id, **DRAFT_CONTENT  # type: ignore[arg-type]
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)

        assert created.bundle() == {}
        assert approve_draft(connection, draft.id).authorizes(created)

    def test_a_bundled_draft_still_needs_a_human(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        """Richer content changes nothing about who decides."""
        draft, _ = self_bundled(drafts, seeded_article)
        assert drafts.get(draft.id).status is DraftStatus.PENDING_REVIEW
        assert authorization_for_approved_draft(connection, draft.id) is None


def self_bundled(drafts: DraftRepository, seeded_article):  # type: ignore[no-untyped-def]
    draft, created = drafts.create(
        article_id=seeded_article.id,
        **DRAFT_CONTENT,  # type: ignore[arg-type]
        prompt_placement=PromptPlacement.COMMENT,
        comment_text=COMMENT,
        media=(image(),),
        footer_text=render_footer(HANDLE, emoji="✨"),
    )
    drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
    return drafts.get(draft.id), created


class TestContentTypes:
    def test_a_use_case_is_not_a_prompt(self) -> None:
        assert ContentType.TESTED_USE_CASE is not ContentType.PROMPT

    def test_every_content_type_is_representable_in_a_draft(self) -> None:
        """Structural, not free text: the type is a column, not a category string."""
        from ai_news_editor.domain.models import Draft

        for content_type in (ContentType.TESTED_USE_CASE, ContentType.RESOURCE):
            draft = Draft(content_type=content_type, content_item_id=uuid4())
            assert draft.content_type is content_type


class TestResourceAndMediaFixtures:
    """The two bundle shapes with no real editorial content behind them yet.

    Neither is production content — a resource with no PDF and an owner-generated image
    that does not exist on disk. They are here to prove the schema, the hashing and the
    review rendering carry them, which is what Phase 8.2 is for. Nothing pretends these
    are real published results.
    """

    def _resource(self) -> ResourceSpec:
        return ResourceSpec(
            resource_type=ResourceType.PDF_COLLECTION,
            title="Збірка перевірених промптів",
            description="Промпти з каналу, зібрані в один файл",
            version="2026-08",
            asset=MediaAsset(
                role=MediaRole.PDF,
                origin=MediaOrigin.EDITORIAL_ASSET,
                reference="resources/prompts-2026-08.pdf",
                description="PDF зі зібраними промптами",
            ),
        )

    def test_a_resource_bundle_is_representable_and_hashed(self) -> None:
        with_resource = version(resource=self._resource())
        assert with_resource.bundle()["resource"]["type"] == "PDF_COLLECTION"
        assert with_resource.content_hash != version().content_hash

    def test_the_resource_identity_covers_its_file(self) -> None:
        """Swapping the attached file must invalidate an approval."""
        one = version(resource=self._resource())
        swapped = self._resource().model_copy(
            update={
                "asset": MediaAsset(
                    role=MediaRole.PDF,
                    origin=MediaOrigin.EDITORIAL_ASSET,
                    reference="resources/something-else.pdf",
                    description="інший файл",
                )
            }
        )
        assert version(resource=swapped).content_hash != one.content_hash

    def test_a_resource_may_have_no_file_at_all(self) -> None:
        """A curated list is the resource; there is nothing to download."""
        spec = ResourceSpec(
            resource_type=ResourceType.CURATED_LIST,
            title="AI-інструменти, які варто спробувати",
            description="Короткий список із поясненнями",
        )
        assert version(resource=spec).bundle()["resource"]["title"] == spec.title

    def test_an_owner_generated_result_image_round_trips(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        asset = image(
            description="результат генерації за промптом із допису",
            tool_used="Gemini",
            model_version=None,
        )
        draft, created = drafts.create(
            article_id=seeded_article.id,
            **DRAFT_CONTENT,  # type: ignore[arg-type]
            media=(asset,),
        )
        reloaded = drafts.current_version(draft.id)

        assert len(reloaded.media) == 1
        assert reloaded.media[0].origin is MediaOrigin.OWNER_GENERATED
        assert reloaded.media[0].tool_used == "Gemini"
        assert reloaded.content_hash == created.content_hash

    def test_a_missing_model_version_is_not_invented(self) -> None:
        """Recorded when known, absent when not. Never guessed."""
        assert image(model_version=None).model_version is None

    def test_the_review_card_shows_media_and_resource(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        from ai_news_editor.bot import render
        from ai_news_editor.review.service import review_queue

        draft, _ = drafts.create(
            article_id=seeded_article.id,
            **DRAFT_CONTENT,  # type: ignore[arg-type]
            media=(image(),),
            resource=self._resource(),
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)

        card = render.review_card(review_queue(connection)[0], position=1, total=1)
        assert "МЕДІА" in card
        assert "RESULT_IMAGE" in card
        assert "зроблено: Gemini" in card
        assert "РЕСУРС" in card
        assert "PDF_COLLECTION" in card

    def test_the_review_card_shows_the_comment_in_full(
        self, connection: sqlite3.Connection, seeded_article, drafts: DraftRepository
    ) -> None:
        """A human approving this post is approving the comment too."""
        from ai_news_editor.bot import render
        from ai_news_editor.review.service import review_queue

        draft, _ = drafts.create(
            article_id=seeded_article.id,
            **DRAFT_CONTENT,  # type: ignore[arg-type]
            prompt_placement=PromptPlacement.COMMENT,
            comment_text=COMMENT,
        )
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)

        card = render.review_card(review_queue(connection)[0], position=1, total=1)
        assert "КОМЕНТАР ДО ПУБЛІКАЦІЇ" in card
        assert COMMENT in card
        assert "COMMENT" in card


class TestUseCaseAndResourceRules:
    """What each of the new formats must and must not carry."""

    def _use_case_body(self, **overrides: object):  # type: ignore[no-untyped-def]
        from ai_news_editor.domain.models import UseCaseBody

        data: dict[str, object] = {
            "what_the_person_did": "склав план дня з хаотичних нотаток",
            "reported_benefit": "перестав губити дрібні задачі",
            "how_to_try": ("почніть з одного дня",),
        }
        data.update(overrides)
        return UseCaseBody(**data)  # type: ignore[arg-type]

    def _evidence(self):  # type: ignore[no-untyped-def]
        from ai_news_editor.domain.enums import SourceTier
        from ai_news_editor.domain.models import PromptEvidence

        return PromptEvidence(
            source_url="https://example.invalid/post",
            source_title="Example",
            source_tier=SourceTier.COMMUNITY_REPORT,
            source_platform="Reddit",
            source_author="u/example",
            tested_by="користувач Reddit",
            tool_used="ChatGPT",
            what_was_tested="перетворити нотатки на план",
            observed_result="автор пише, що план вийшов придатним",
        )

    def _item(self, **overrides: object):  # type: ignore[no-untyped-def]
        from ai_news_editor.domain.enums import EvidenceStatus, UseCaseTheme
        from ai_news_editor.domain.models import ContentItem

        data: dict[str, object] = {
            "content_type": ContentType.TESTED_USE_CASE,
            "audience": AudienceTier.NEWCOMER,
            "title": "t",
            "use_case_theme": UseCaseTheme.ORGANIZATION,
            "body": self._use_case_body(),
            "evidence": self._evidence(),
            "evidence_status": EvidenceStatus.VERIFIED_SOURCE_BACKED,
            "created_by": "claude-code",
        }
        data.update(overrides)
        return ContentItem(**data)  # type: ignore[arg-type]

    def test_a_complete_use_case_is_accepted(self) -> None:
        item = self._item()
        assert item.subject == "ORGANIZATION"

    def test_a_use_case_without_evidence_is_refused(self) -> None:
        """Without it, it is just a story we made up."""
        with pytest.raises(ValidationError, match="story we made up"):
            self._item(evidence=None, evidence_status=None)

    def test_a_use_case_needs_an_evidence_status(self) -> None:
        with pytest.raises(ValidationError, match="needs an evidence status"):
            self._item(evidence_status=None)

    def test_a_use_case_needs_a_use_case_body(self) -> None:
        from ai_news_editor.domain.models import ExplainerBody

        with pytest.raises(ValidationError, match="use-case body"):
            self._item(
                body=ExplainerBody(
                    concept="c", simple_explanation="s", real_life_example="r",
                    why_it_matters="w",
                )
            )

    def test_a_theme_belongs_only_to_a_use_case(self) -> None:
        from ai_news_editor.domain.enums import UseCaseTheme
        from ai_news_editor.domain.models import ExplainerBody

        with pytest.raises(ValidationError, match="belongs to a TESTED_USE_CASE"):
            self._item(
                content_type=ContentType.EXPLAINER,
                use_case_theme=UseCaseTheme.STUDY,
                body=ExplainerBody(
                    concept="c", simple_explanation="s", real_life_example="r",
                    why_it_matters="w",
                ),
                evidence=None,
                evidence_status=None,
            )

    def test_a_resource_carries_no_tested_evidence(self) -> None:
        """Curated material is not a claim that a workflow worked."""
        from ai_news_editor.domain.models import ResourceBody

        with pytest.raises(ValidationError, match="not a report of a tested workflow"):
            self._item(
                content_type=ContentType.RESOURCE,
                use_case_theme=None,
                body=ResourceBody(
                    spec=ResourceSpec(
                        resource_type=ResourceType.CHECKLIST, title="t", description="d"
                    ),
                    what_it_gives_you="x",
                    how_to_use=("y",),
                ),
                evidence_status=None,
            )

    def test_a_resource_needs_a_resource_body(self) -> None:
        with pytest.raises(ValidationError, match="resource body"):
            self._item(
                content_type=ContentType.RESOURCE,
                use_case_theme=None,
                evidence=None,
                evidence_status=None,
            )

    def test_a_series_needs_both_a_name_and_a_position(self) -> None:
        with pytest.raises(ValidationError, match="name and a position"):
            self._item(series_name="7 днів AI-креативів")
        with pytest.raises(ValidationError, match="name and a position"):
            self._item(series_order=3)

    def test_a_complete_series_reads_back_as_a_label(self) -> None:
        item = self._item(series_name="7 днів AI-креативів", series_order=3)
        assert item.series_label == "7 днів AI-креативів · 3"

    def test_no_series_means_no_label(self) -> None:
        assert self._item().series_label is None

    def test_a_resource_subject_is_its_type(self) -> None:
        from ai_news_editor.domain.models import ResourceBody

        item = self._item(
            content_type=ContentType.RESOURCE,
            use_case_theme=None,
            evidence=None,
            evidence_status=None,
            body=ResourceBody(
                spec=ResourceSpec(
                    resource_type=ResourceType.CHEAT_SHEET, title="t", description="d"
                ),
                what_it_gives_you="x",
                how_to_use=("y",),
            ),
        )
        assert item.subject == "CHEAT_SHEET"

    def test_the_social_platform_and_handle_are_preserved(self) -> None:
        """Social posts vanish; this is what was reviewed."""
        evidence = self._evidence()
        assert evidence.source_platform == "Reddit"
        assert evidence.source_author == "u/example"
