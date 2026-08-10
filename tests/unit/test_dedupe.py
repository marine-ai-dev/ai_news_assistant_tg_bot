"""Editorial duplicate detection and canonical selection.

The recurring theme is false-positive resistance: wrongly merging two stories removes
one from the pool without anyone noticing, which is far worse than letting a duplicate
reach the editor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ai_news_editor.domain.enums import DuplicateReason, TrustTier
from ai_news_editor.domain.models import Article, DuplicateCandidate
from ai_news_editor.pipeline.dedupe import NEAR_DUPLICATE_WINDOW, find_duplicate, prefer_canonical
from ai_news_editor.pipeline.fingerprint import content_fingerprint, simhash, title_fingerprint

WHEN = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
LONG_TEXT = (
    "OpenAI launches a new ChatGPT feature that lets ordinary users build custom agents "
    "without writing any code at all"
)


def article(**overrides: object) -> Article:
    title = overrides.pop("title", "A headline about an AI product update")  # type: ignore[assignment]
    text = overrides.pop("text", LONG_TEXT)
    data: dict[str, object] = {
        "raw_item_id": uuid4(),
        "source_id": "alpha",
        "title": title,
        "canonical_url": "https://alpha.invalid/a",
        "clean_text": text,
        "published_at": WHEN,
        "content_hash": content_fingerprint(title, text),  # type: ignore[arg-type]
        "title_fingerprint": title_fingerprint(title),  # type: ignore[arg-type]
        "simhash": simhash(f"{title}\n{text}"),
    }
    data.update(overrides)
    return Article.model_validate(data)


def candidate(**overrides: object) -> DuplicateCandidate:
    title = overrides.pop("title", "A headline about an AI product update")
    text = overrides.pop("text", LONG_TEXT)
    data: dict[str, object] = {
        "id": uuid4(),
        "source_id": "alpha",
        "canonical_url": "https://alpha.invalid/a",
        "trust_tier": TrustTier.OFFICIAL,
        "content_hash": content_fingerprint(title, text),  # type: ignore[arg-type]
        "title_fingerprint": title_fingerprint(title),  # type: ignore[arg-type]
        "simhash": simhash(f"{title}\n{text}"),
        "published_at": WHEN,
        "text_length": len(str(text)),
    }
    data.update(overrides)
    return DuplicateCandidate.model_validate(data)


class TestExactLayers:
    def test_same_canonical_url_is_a_duplicate(self) -> None:
        match = find_duplicate(article(), [candidate(canonical_url="https://alpha.invalid/a")])
        assert match is not None
        assert match.reason is DuplicateReason.SAME_CANONICAL_URL

    def test_same_content_fingerprint_is_a_duplicate(self) -> None:
        match = find_duplicate(article(), [candidate(canonical_url="https://alpha.invalid/other")])
        assert match is not None
        assert match.reason is DuplicateReason.SAME_CONTENT_FINGERPRINT

    def test_same_title_from_the_same_source_is_a_duplicate(self) -> None:
        match = find_duplicate(
            article(),
            [
                candidate(
                    canonical_url="https://alpha.invalid/other",
                    text="A completely different body with plenty of unrelated words in it here",
                )
            ],
        )
        assert match is not None
        assert match.reason is DuplicateReason.SAME_TITLE_SAME_SOURCE

    def test_same_title_from_a_different_source_is_not_a_title_duplicate(self) -> None:
        """Two outlets can legitimately use the same headline for their own coverage."""
        match = find_duplicate(
            article(),
            [
                candidate(
                    source_id="beta",
                    canonical_url="https://beta.invalid/other",
                    text="Totally unrelated wording that shares nothing with the original story",
                )
            ],
        )
        assert match is None or match.reason is not DuplicateReason.SAME_TITLE_SAME_SOURCE

    def test_url_match_wins_over_weaker_signals(self) -> None:
        match = find_duplicate(
            article(),
            [
                candidate(canonical_url="https://alpha.invalid/other"),
                candidate(canonical_url="https://alpha.invalid/a"),
            ],
        )
        assert match is not None
        assert match.reason is DuplicateReason.SAME_CANONICAL_URL


class TestNearDuplicates:
    def test_reworded_text_is_caught(self) -> None:
        reworded = LONG_TEXT.replace("that", "which") + "!"
        match = find_duplicate(
            article(),
            [
                candidate(
                    canonical_url="https://alpha.invalid/other",
                    title="Slightly different headline",
                    text=reworded,
                )
            ],
        )
        assert match is not None
        assert match.reason is DuplicateReason.NEAR_DUPLICATE_SIMHASH

    def test_unrelated_stories_are_not_matched(self) -> None:
        match = find_duplicate(
            article(),
            [
                candidate(
                    canonical_url="https://alpha.invalid/other",
                    title="Something else entirely",
                    text="Researchers describe a novel CUDA kernel optimisation for sparse "
                    "matrix multiplication on modern GPUs",
                )
            ],
        )
        assert match is None

    def test_short_texts_are_never_near_matched(self) -> None:
        """Short unrelated texts collide easily, so they are excluded entirely."""
        subject = article(
            title="Short one", text="tiny body", canonical_url="https://alpha.invalid/x"
        )
        assert subject.simhash is None
        assert (
            find_duplicate(
                subject,
                [
                    candidate(
                        canonical_url="https://alpha.invalid/y", title="Short two", text="tiny text"
                    )
                ],
            )
            is None
        )

    def test_recurring_monthly_column_is_not_a_duplicate(self) -> None:
        """A real false positive from live data.

        Vendors publish "the latest AI news from <month>" every month. The texts differ
        by one word, but they are genuinely different stories — the publication dates
        are what separate them.
        """
        may = article(
            title="The latest AI news we announced in May 2026",
            text="Here are Google's latest AI updates from May 2026 covering many products",
            canonical_url="https://alpha.invalid/may",
            published_at=datetime(2026, 5, 30, tzinfo=UTC),
        )
        july = candidate(
            title="The latest AI news we announced in July 2026",
            text="Here are Google's latest AI updates from July 2026 covering many products",
            canonical_url="https://alpha.invalid/july",
            published_at=datetime(2026, 7, 31, tzinfo=UTC),
        )
        assert find_duplicate(may, [july]) is None

    def test_the_same_story_within_the_window_is_still_caught(self) -> None:
        subject = article(published_at=WHEN, canonical_url="https://alpha.invalid/x")
        near = candidate(
            canonical_url="https://alpha.invalid/y",
            title="Different headline entirely here",
            text=LONG_TEXT.replace("that", "which"),
            published_at=WHEN + timedelta(days=1),
        )
        match = find_duplicate(subject, [near])
        assert match is not None
        assert match.reason is DuplicateReason.NEAR_DUPLICATE_SIMHASH

    def test_window_boundary(self) -> None:
        subject = article(published_at=WHEN, canonical_url="https://alpha.invalid/x")
        outside = candidate(
            canonical_url="https://alpha.invalid/y",
            title="Different headline entirely here",
            text=LONG_TEXT.replace("that", "which"),
            published_at=WHEN + NEAR_DUPLICATE_WINDOW + timedelta(days=1),
        )
        assert find_duplicate(subject, [outside]) is None

    def test_unknown_dates_do_not_block_matching(self) -> None:
        subject = article(published_at=None, canonical_url="https://alpha.invalid/x")
        near = candidate(
            canonical_url="https://alpha.invalid/y",
            title="Different headline entirely here",
            text=LONG_TEXT.replace("that", "which"),
            published_at=None,
        )
        assert find_duplicate(subject, [near]) is not None

    def test_closest_neighbour_wins(self) -> None:
        subject = article(canonical_url="https://alpha.invalid/x")
        exact_text = candidate(
            canonical_url="https://alpha.invalid/y", title="Other headline here now", text=LONG_TEXT
        )
        match = find_duplicate(subject, [exact_text])
        assert match is not None
        assert match.duplicate_of_id == exact_text.id


class TestCrossSource:
    def test_cross_source_matches_are_flagged(self) -> None:
        match = find_duplicate(
            article(), [candidate(source_id="beta", canonical_url="https://alpha.invalid/a")]
        )
        assert match is not None
        assert match.cross_source is True

    def test_same_source_matches_are_not_flagged(self) -> None:
        match = find_duplicate(article(), [candidate(canonical_url="https://alpha.invalid/a")])
        assert match is not None
        assert match.cross_source is False


class TestNoMatch:
    def test_empty_candidate_list(self) -> None:
        assert find_duplicate(article(), []) is None

    def test_an_article_is_never_its_own_duplicate(self) -> None:
        subject = article()
        self_candidate = candidate(id=subject.id, canonical_url=subject.canonical_url)
        assert find_duplicate(subject, [self_candidate]) is None


class TestCanonicalSelection:
    def _c(self, **kw: object) -> DuplicateCandidate:
        return candidate(**kw)

    def test_official_beats_secondary(self) -> None:
        official = self._c(trust_tier=TrustTier.OFFICIAL, id=uuid4())
        secondary = self._c(trust_tier=TrustTier.REPUTABLE_SECONDARY, id=uuid4())
        assert prefer_canonical(secondary, official) is official
        assert prefer_canonical(official, secondary) is official

    def test_community_signal_never_wins(self) -> None:
        """Community chatter is not a factual source and can never be canonical."""
        community = self._c(trust_tier=TrustTier.COMMUNITY_SIGNAL, id=uuid4())
        for tier in (TrustTier.OFFICIAL, TrustTier.REPUTABLE_SECONDARY, TrustTier.UNVERIFIED):
            other = self._c(trust_tier=tier, id=uuid4())
            assert prefer_canonical(community, other) is other

    def test_earlier_publication_wins_within_a_tier(self) -> None:
        early = self._c(published_at=WHEN, id=uuid4())
        late = self._c(published_at=WHEN + timedelta(days=1), id=uuid4())
        assert prefer_canonical(late, early) is early

    def test_known_date_beats_unknown(self) -> None:
        known = self._c(published_at=WHEN, id=uuid4())
        unknown = self._c(published_at=None, id=uuid4())
        assert prefer_canonical(unknown, known) is known

    def test_richer_text_wins_as_a_later_tiebreak(self) -> None:
        rich = self._c(text_length=5000, id=uuid4())
        thin = self._c(text_length=10, id=uuid4())
        assert prefer_canonical(thin, rich) is rich

    def test_result_is_deterministic_and_order_independent(self) -> None:
        left = self._c(id=uuid4(), text_length=100)
        right = self._c(id=uuid4(), text_length=100)
        assert prefer_canonical(left, right) is prefer_canonical(right, left)

    def test_otherwise_identical_candidates_break_ties_by_id(self) -> None:
        """A stable tie-break, so the canonical choice never depends on run order."""
        a = self._c(id=uuid4(), text_length=50)
        b = self._c(id=uuid4(), text_length=50)
        assert str(prefer_canonical(a, b).id) == min(str(a.id), str(b.id))
