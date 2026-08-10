"""The rubric: weights, gates, ranking, and the schema's coherence rules."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_news_editor.domain.enums import (
    SENSITIVE_CATEGORIES,
    AudienceTier,
    Category,
    EditorialDecision,
    TrustTier,
    VerificationStatus,
)
from ai_news_editor.editorial.rubric import (
    AI_RELEVANCE_SHORTLIST_THRESHOLD,
    CREDIBILITY_SHORTLIST_THRESHOLD,
    DIMENSIONS,
    WEIGHTS,
    composite_score,
    passes_credibility_gate,
    verification_is_sufficient,
)
from ai_news_editor.editorial.schema import ArticleReview, Scores
from tests.conftest import make_review, scores_for


class TestWeights:
    def test_weights_sum_to_one(self) -> None:
        assert round(sum(WEIGHTS.values()), 6) == 1.0

    def test_every_weighted_dimension_is_a_real_dimension(self) -> None:
        assert set(WEIGHTS) <= set(DIMENSIONS)

    def test_gates_are_not_weighted(self) -> None:
        """A story cannot compensate for being unreliable or off-topic by being fun."""
        assert "credibility" not in WEIGHTS
        assert "general_ai_relevance" not in WEIGHTS

    def test_reader_interest_dominates(self) -> None:
        """The channel's thesis: reader interest outranks everything else."""
        assert WEIGHTS["reader_interest"] == max(WEIGHTS.values())


class TestCompositeScore:
    def test_all_zero(self) -> None:
        assert composite_score(scores_for(0)) == 0.0

    def test_all_hundred(self) -> None:
        assert composite_score(scores_for(100)) == 100.0

    def test_is_deterministic(self) -> None:
        scores = scores_for(50, reader_interest=90)
        assert composite_score(scores) == composite_score(scores)

    def test_credibility_does_not_move_the_score(self) -> None:
        low = composite_score(scores_for(60, credibility=10))
        high = composite_score(scores_for(60, credibility=100))
        assert low == high

    def test_reader_interest_moves_the_score(self) -> None:
        assert composite_score(scores_for(50, reader_interest=100)) > composite_score(
            scores_for(50, reader_interest=0)
        )


class TestConsumerVersusTechnicalRanking:
    """The channel's defining ranking expectation, produced by scoring alone."""

    def test_a_consumer_feature_outranks_an_infrastructure_story(self) -> None:
        # An honest scoring of "CUDA optimisation improves kernel throughput by 11%":
        # true and novel, but almost nobody outside the field can use it.
        infrastructure = scores_for(
            0,
            credibility=95,
            general_ai_relevance=85,
            reader_interest=15,
            usefulness=10,
            novelty=70,
            wow_factor=20,
            virality_potential=10,
            accessibility=15,
            consumer_impact=10,
        )
        # "ChatGPT launches a feature millions can use today."
        consumer = scores_for(
            0,
            credibility=95,
            general_ai_relevance=90,
            reader_interest=92,
            usefulness=90,
            novelty=70,
            wow_factor=65,
            virality_potential=75,
            accessibility=95,
            consumer_impact=92,
        )
        assert composite_score(consumer) > composite_score(infrastructure) + 40

    def test_nothing_keys_on_any_keyword(self) -> None:
        """Identical scores rank identically whatever the story is about."""
        assert composite_score(scores_for(70)) == composite_score(scores_for(70))


class TestCredibilityGate:
    def test_high_credibility_passes(self) -> None:
        assert passes_credibility_gate(scores_for(80))

    def test_low_credibility_fails(self) -> None:
        assert not passes_credibility_gate(scores_for(80, credibility=20))

    def test_threshold_boundary(self) -> None:
        assert passes_credibility_gate(scores_for(80, credibility=CREDIBILITY_SHORTLIST_THRESHOLD))
        assert not passes_credibility_gate(
            scores_for(80, credibility=CREDIBILITY_SHORTLIST_THRESHOLD - 1)
        )

    def test_off_topic_fails_even_when_credible(self) -> None:
        assert not passes_credibility_gate(
            scores_for(90, general_ai_relevance=AI_RELEVANCE_SHORTLIST_THRESHOLD - 1)
        )

    def test_an_exciting_but_unreliable_story_cannot_be_shortlisted(self) -> None:
        """The invariant the gate exists for."""
        scores = scores_for(
            50,
            credibility=20,
            reader_interest=100,
            wow_factor=100,
            virality_potential=100,
        )
        assert not passes_credibility_gate(scores)
        with pytest.raises(ValidationError, match="credibility 20"):
            make_review(decision=EditorialDecision.SHORTLIST, scores=scores)

    def test_the_same_story_may_be_held(self) -> None:
        """Rejecting outright would lose it; holding keeps it for someone to check."""
        review = make_review(
            decision=EditorialDecision.HOLD_FOR_VERIFICATION,
            scores=scores_for(50, credibility=20, reader_interest=100),
            verification_status=VerificationStatus.NEEDS_MORE_EVIDENCE,
        )
        assert review.decision is EditorialDecision.HOLD_FOR_VERIFICATION


class TestVerificationRules:
    def test_ordinary_official_announcement_needs_no_verification(self) -> None:
        assert (
            verification_is_sufficient(
                EditorialDecision.SHORTLIST,
                VerificationStatus.NOT_REQUIRED,
                sensitive=False,
                corroborating_sources=0,
            )
            is None
        )

    def test_incomplete_verification_cannot_be_shortlisted(self) -> None:
        problem = verification_is_sufficient(
            EditorialDecision.SHORTLIST,
            VerificationStatus.NEEDS_MORE_EVIDENCE,
            sensitive=False,
            corroborating_sources=0,
        )
        assert problem is not None
        assert "HOLD_FOR_VERIFICATION" in problem

    def test_a_sensitive_story_always_requires_verification(self) -> None:
        problem = verification_is_sufficient(
            EditorialDecision.SHORTLIST,
            VerificationStatus.NOT_REQUIRED,
            sensitive=True,
            corroborating_sources=0,
        )
        assert problem is not None
        assert "sensitive" in problem

    def test_claiming_verified_without_sources_is_refused(self) -> None:
        problem = verification_is_sufficient(
            EditorialDecision.SHORTLIST,
            VerificationStatus.VERIFIED,
            sensitive=True,
            corroborating_sources=0,
        )
        assert problem is not None
        assert "no verification_sources" in problem

    def test_a_verified_sensitive_story_is_acceptable(self) -> None:
        assert (
            verification_is_sufficient(
                EditorialDecision.SHORTLIST,
                VerificationStatus.VERIFIED,
                sensitive=True,
                corroborating_sources=2,
            )
            is None
        )

    def test_rules_do_not_constrain_rejections(self) -> None:
        assert (
            verification_is_sufficient(
                EditorialDecision.REJECT,
                VerificationStatus.NEEDS_MORE_EVIDENCE,
                sensitive=True,
                corroborating_sources=0,
            )
            is None
        )


class TestSensitiveStoryEndToEnd:
    @pytest.mark.parametrize("category", sorted(SENSITIVE_CATEGORIES))
    def test_a_sensitive_shortlist_without_verification_is_refused(
        self, category: Category
    ) -> None:
        with pytest.raises(ValidationError, match="sensitive"):
            make_review(
                decision=EditorialDecision.SHORTLIST,
                category=category,
                verification_status=VerificationStatus.NOT_REQUIRED,
            )

    def test_a_viral_deepfake_with_thin_evidence_is_held(self) -> None:
        review = make_review(
            decision=EditorialDecision.HOLD_FOR_VERIFICATION,
            category=Category.DEEPFAKE_WATCH,
            scores=scores_for(60, credibility=45, reader_interest=95, virality_potential=98),
            verification_status=VerificationStatus.NEEDS_MORE_EVIDENCE,
        )
        assert review.decision is EditorialDecision.HOLD_FOR_VERIFICATION

    def test_a_corroborated_deepfake_story_can_be_shortlisted(self) -> None:
        review = make_review(
            decision=EditorialDecision.SHORTLIST,
            category=Category.DEEPFAKE_WATCH,
            scores=scores_for(80),
            verification_status=VerificationStatus.VERIFIED,
            verification_sources=[
                {
                    "url": "https://reuters.invalid/story",
                    "source_name": "Reuters",
                    "source_type": "REPUTABLE_SECONDARY",
                }
            ],
        )
        assert review.decision is EditorialDecision.SHORTLIST


class TestShortlistCompleteness:
    def test_a_shortlist_must_explain_itself(self) -> None:
        with pytest.raises(ValidationError, match="why it was selected"):
            make_review(decision=EditorialDecision.SHORTLIST, why_selected=[])

    def test_a_shortlist_must_carry_an_angle(self) -> None:
        with pytest.raises(ValidationError, match="editorial_angle"):
            make_review(decision=EditorialDecision.SHORTLIST, editorial_angle="  ")

    def test_a_rejection_needs_neither(self) -> None:
        review = make_review(
            decision=EditorialDecision.REJECT, why_selected=[], editorial_angle=None
        )
        assert review.decision is EditorialDecision.REJECT


class TestVerificationSources:
    def test_community_signal_cannot_verify(self) -> None:
        """Community discussion points at a story; it never settles whether it is true."""
        with pytest.raises(ValidationError, match="cannot serve as verification"):
            make_review(
                verification_sources=[
                    {
                        "url": "https://news.ycombinator.com/item?id=1",
                        "source_name": "Hacker News",
                        "source_type": TrustTier.COMMUNITY_SIGNAL.value,
                    }
                ]
            )

    def test_official_and_reputable_sources_qualify(self) -> None:
        for tier in (TrustTier.OFFICIAL, TrustTier.REPUTABLE_SECONDARY):
            review = make_review(
                verification_status=VerificationStatus.VERIFIED,
                verification_sources=[
                    {"url": "https://x.invalid", "source_name": "X", "source_type": tier.value}
                ],
            )
            assert review.verification_sources


class TestScoreBounds:
    @pytest.mark.parametrize("value", [-1, 101, 1000])
    def test_out_of_range_scores_are_refused(self, value: int) -> None:
        with pytest.raises(ValidationError):
            Scores.model_validate(scores_for(50) | {"reader_interest": value})

    def test_missing_dimension_is_refused(self) -> None:
        incomplete = scores_for(50)
        del incomplete["novelty"]
        with pytest.raises(ValidationError):
            Scores.model_validate(incomplete)

    def test_unknown_dimension_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Scores.model_validate(scores_for(50) | {"vibes": 90})


class TestControlledVocabularies:
    def test_invalid_decision_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ArticleReview.model_validate(
                {**make_review().model_dump(mode="json"), "decision": "PUBLISH_NOW"}
            )

    def test_invalid_category_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ArticleReview.model_validate(
                {**make_review().model_dump(mode="json"), "category": "SOMETHING_NEW"}
            )

    def test_invalid_audience_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ArticleReview.model_validate(
                {**make_review().model_dump(mode="json"), "audience": "EXPERTS"}
            )

    def test_valid_audiences(self) -> None:
        for audience in AudienceTier:
            assert make_review(audience=audience).audience is audience
