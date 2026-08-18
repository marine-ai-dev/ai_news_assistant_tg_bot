"""editorial.diversity — the exact behavioural examples from Step 3 section 25.

Tests assert ordering/behaviour, not exact score values, per the module's own design
brief: "write tests around behaviour, not exact meaningless score values."
"""

from __future__ import annotations

from pathlib import Path

from ai_news_editor.domain.enums import EditorialCategory
from ai_news_editor.editorial.diversity import (
    DiversityWeights,
    RecentPost,
    diversity_adjustment,
    rank,
)

NEWS = EditorialCategory.NEWS
AI_TOOL = EditorialCategory.AI_TOOL


class TestExampleA_CategoryRepetition:
    """recent: NEWS/Google, NEWS/Google. available: NEWS/Google, AI_TOOL/Anthropic
    (equal base quality). -> prefer AI_TOOL/Anthropic."""

    def test_the_non_repeating_category_is_ranked_first(self) -> None:
        recent = [RecentPost(NEWS, "Google"), RecentPost(NEWS, "Google")]
        candidates = [
            ("google_story", NEWS, "Google", 80.0),
            ("anthropic_story", AI_TOOL, "Anthropic", 80.0),
        ]
        ranked = rank(candidates, recent)
        assert ranked[0].candidate == "anthropic_story"


class TestExampleB_OnlyTrustworthyCandidateStillWins:
    """recent: AI_TOOL/Anthropic. only trustworthy candidate: NEWS/Google. -> NEWS
    still allowed — diversity never removes a candidate, only reorders peers."""

    def test_a_lone_candidate_is_never_excluded_by_diversity(self) -> None:
        recent = [RecentPost(AI_TOOL, "Anthropic")]
        candidates = [("google_story", NEWS, "Google", 70.0)]
        ranked = rank(candidates, recent)
        assert len(ranked) == 1
        assert ranked[0].candidate == "google_story"

    def test_repeating_the_last_post_still_leaves_a_positive_final_score(self) -> None:
        """Even in the worst case — repeating category AND source family — the
        adjustment is a nudge, not a rejection: base score dominates for anything
        reasonably strong."""
        recent = [RecentPost(NEWS, "Google")] * 3
        adjustment = diversity_adjustment(category=NEWS, source_family="Google", recent=recent)
        assert 80.0 + adjustment > 0


class TestExampleC_SourceFamilyRepetition:
    """recent: Google, Google. available (equal quality, same category): Google,
    Microsoft. -> Microsoft preferred."""

    def test_the_non_repeating_source_family_is_ranked_first(self) -> None:
        recent = [RecentPost(NEWS, "Google"), RecentPost(NEWS, "Google")]
        candidates = [
            ("google_story", NEWS, "Google", 75.0),
            ("microsoft_story", NEWS, "Microsoft", 75.0),
        ]
        ranked = rank(candidates, recent)
        assert ranked[0].candidate == "microsoft_story"

    def test_google_is_not_banned_only_deprioritized(self) -> None:
        """No permanent ban — a strong-enough Google candidate can still win."""
        recent = [RecentPost(NEWS, "Google"), RecentPost(NEWS, "Google")]
        candidates = [
            ("weak_microsoft", NEWS, "Microsoft", 40.0),
            ("strong_google", NEWS, "Google", 95.0),
        ]
        ranked = rank(candidates, recent)
        assert ranked[0].candidate == "strong_google"


class TestExampleD_IndependentFromDomainCooldown:
    """Domain cooldown (automation.pipeline) is an HTTP-reliability mechanism that
    excludes a domain outright for the rest of one run. Editorial diversity is a
    ranking preference that never excludes anything. The two must stay conceptually
    and structurally separate."""

    def test_this_module_does_not_import_the_automation_pipeline(self) -> None:
        """The module docstring *discusses* automation.pipeline in prose (explaining
        why the two stay separate) — that mention is fine. What must never appear is
        an actual import of it, or its cooldown internals."""
        import ai_news_editor.editorial.diversity as diversity_module

        text = Path(diversity_module.__file__).read_text(encoding="utf-8")
        assert "import ai_news_editor.automation" not in text
        assert "from ai_news_editor.automation" not in text
        assert "_domain_of" not in text
        assert "blocked_domains" not in text

    def test_a_repetition_penalty_never_removes_the_candidate_from_the_ranking(self) -> None:
        """Unlike domain cooldown (which filters `remaining`), diversity always
        returns every candidate it was given — just reordered."""
        recent = [RecentPost(NEWS, "Google")] * 5
        candidates = [
            ("a", NEWS, "Google", 10.0),
            ("b", NEWS, "Google", 10.0),
            ("c", NEWS, "Google", 10.0),
        ]
        ranked = rank(candidates, recent)
        assert {s.candidate for s in ranked} == {"a", "b", "c"}


class TestNoRepetitionNoAdjustment:
    def test_an_empty_history_applies_no_penalty(self) -> None:
        assert diversity_adjustment(category=NEWS, source_family="Google", recent=[]) == 0.0

    def test_a_single_differing_post_in_the_window_clears_the_penalty(self) -> None:
        """Unanimous, not majority: 2-of-3 matching is not enough."""
        recent = [
            RecentPost(NEWS, "Google"),
            RecentPost(AI_TOOL, "Anthropic"),
            RecentPost(NEWS, "Google"),
        ]
        assert diversity_adjustment(category=NEWS, source_family="Google", recent=recent) == 0.0

    def test_only_the_lookback_window_is_consulted(self) -> None:
        """A repetition further back than `lookback` does not count."""
        recent = [RecentPost(NEWS, "Google")] * 2 + [RecentPost(AI_TOOL, "Anthropic")] * 3
        weights = DiversityWeights(lookback=3)
        # Last 3 are all Anthropic/AI_TOOL — a NEWS/Google candidate sees no overlap.
        assert diversity_adjustment(
            category=NEWS, source_family="Google", recent=recent, weights=weights
        ) == 0.0


class TestWeightsAreCentralized:
    def test_default_weights_are_a_single_small_object(self) -> None:
        from ai_news_editor.editorial.diversity import DEFAULT_WEIGHTS

        assert isinstance(DEFAULT_WEIGHTS, DiversityWeights)

    def test_custom_weights_can_be_passed_through_explicitly(self) -> None:
        recent = [RecentPost(NEWS, "Google")] * 3
        weights = DiversityWeights(category_repetition_penalty=1000.0, lookback=3)
        adjustment = diversity_adjustment(
            category=NEWS, source_family=None, recent=recent, weights=weights
        )
        assert adjustment == -1000.0
