"""Domain model validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_news_editor.domain.clock import ensure_utc, from_iso, to_iso
from ai_news_editor.domain.content import compute_content_hash
from ai_news_editor.domain.enums import AudienceTier, Category, TrustTier
from ai_news_editor.domain.models import Article, Draft, DraftVersion, RawItem, Source


class TestTimestamps:
    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="naive datetime"):
            RawItem(
                source_id="s",
                url_original="https://example.invalid/x",
                payload_raw="{}",
                fetched_at=datetime(2026, 1, 1, 12, 0),
            )

    def test_offset_datetime_is_normalised_to_utc(self) -> None:
        kyiv = timezone(timedelta(hours=3))
        item = RawItem(
            source_id="s",
            url_original="https://example.invalid/x",
            payload_raw="{}",
            fetched_at=datetime(2026, 1, 1, 15, 0, tzinfo=kyiv),
        )
        assert item.fetched_at.tzinfo is UTC
        assert item.fetched_at.hour == 12

    def test_iso_round_trip(self) -> None:
        original = datetime(2026, 5, 4, 3, 2, 1, tzinfo=UTC)
        assert from_iso(to_iso(original)) == original

    def test_ensure_utc_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            ensure_utc(datetime(2026, 1, 1))


class TestSource:
    def test_community_signal_must_be_signal_only(self) -> None:
        with pytest.raises(ValidationError, match="signal_only"):
            Source(
                id="forum",
                name="Forum",
                kind="RSS",
                url="https://example.invalid",
                trust_tier=TrustTier.COMMUNITY_SIGNAL,
                signal_only=False,
            )

    def test_community_signal_accepted_when_marked(self) -> None:
        source = Source(
            id="forum",
            name="Forum",
            kind="RSS",
            url="https://example.invalid",
            trust_tier=TrustTier.COMMUNITY_SIGNAL,
            signal_only=True,
        )
        assert source.signal_only is True

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Source(
                id="s",
                name="S",
                kind="RSS",
                url="https://example.invalid",
                trust_tier=TrustTier.OFFICIAL,
                typo_field="oops",
            )


class TestArticle:
    def test_article_cannot_duplicate_itself(self) -> None:
        article_id = uuid4()
        with pytest.raises(ValidationError, match="duplicate of itself"):
            Article(
                id=article_id,
                raw_item_id=uuid4(),
                source_id="s",
                title="T",
                canonical_url="https://example.invalid/x",
                duplicate_of_id=article_id,
            )

    def test_empty_title_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Article(
                raw_item_id=uuid4(),
                source_id="s",
                title="",
                canonical_url="https://example.invalid/x",
            )


class TestDraftVersion:
    def _version(self, **overrides: object) -> DraftVersion:
        data: dict[str, object] = {
            "draft_id": uuid4(),
            "version_no": 1,
            "title": "Title",
            "body": "Body",
            "category": Category.PRODUCT_UPDATE,
            "audience": AudienceTier.BEGINNER,
            "source_attribution": "Example",
            "created_by": "test",
        }
        data.update(overrides)
        return DraftVersion.model_validate(data)

    def test_content_hash_is_derived_not_supplied(self) -> None:
        """A version whose hash disagrees with its text must be unrepresentable."""
        with pytest.raises(ValidationError):
            self._version(content_hash="0" * 64)

    def test_identical_content_hashes_identically(self) -> None:
        draft_id = uuid4()
        a = self._version(draft_id=draft_id)
        b = self._version(draft_id=draft_id)
        assert a.id != b.id
        assert a.content_hash == b.content_hash

    @pytest.mark.parametrize(
        "field,value",
        [
            ("title", "Different"),
            ("body", "Different"),
            ("category", Category.AI_FAIL),
            ("audience", AudienceTier.TECH_CURIOUS),
            ("source_attribution", "Other"),
            ("hashtags", ("#ai",)),
        ],
    )
    def test_every_visible_field_changes_the_hash(self, field: str, value: object) -> None:
        baseline = self._version()
        assert self._version(**{field: value}).content_hash != baseline.content_hash

    def test_hashtag_order_matters(self) -> None:
        first = self._version(hashtags=("#a", "#b"))
        second = self._version(hashtags=("#b", "#a"))
        assert first.content_hash != second.content_hash

    def test_version_is_frozen(self) -> None:
        version = self._version()
        with pytest.raises(ValidationError):
            version.body = "mutated"  # type: ignore[misc]

    def test_version_no_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            self._version(version_no=0)


class TestContentHash:
    def test_is_stable_across_calls(self) -> None:
        kwargs = {
            "title": "T",
            "body": "B",
            "hashtags": ["#x"],
            "category": "WOW",
            "audience": "GENERAL",
            "source_attribution": "A",
        }
        assert compute_content_hash(**kwargs) == compute_content_hash(**kwargs)  # type: ignore[arg-type]

    def test_handles_non_ascii(self) -> None:
        digest = compute_content_hash(
            title="Новина",
            body="Текст українською",
            hashtags=["#штучнийінтелект"],
            category="WOW",
            audience="BEGINNER",
            source_attribution="Джерело",
        )
        assert len(digest) == 64


class TestDraft:
    def test_new_draft_has_no_current_version(self) -> None:
        assert Draft(article_id=uuid4()).current_version_id is None
