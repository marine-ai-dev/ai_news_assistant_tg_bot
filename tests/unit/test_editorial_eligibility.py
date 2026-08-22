"""automation.eligibility — AI News Agent v3 priority step.

Pure, deterministic tests: every case here constructs the flags directly, as if a
Gemini classification call had already answered them, and asserts the eligibility
decision — no Gemini, no network, no database. The test matrix mirrors the priority
step's own MUST REJECT / MUST PASS / EDGE CASES list.
"""

from __future__ import annotations

import itertools

import pytest

from ai_news_editor.automation.eligibility import (
    REJECT_CYBERSECURITY,
    REJECT_DEFTECH,
    REJECT_FORBIDDEN_GEOGRAPHY,
    REJECT_GENERIC_DEVTECH,
    REJECT_GENERIC_GOVERNMENT,
    REJECT_MILTECH,
    REJECT_NOT_AI_FIRST,
    REJECT_POLITICS,
    REJECT_SPECULATIVE_DOOM,
    REJECT_WAR,
    evaluate_eligibility,
)


class TestEligibleIsExactlyNoRejectionCodes:
    def test_all_flags_false_and_ai_primary_true_is_eligible(self) -> None:
        result = evaluate_eligibility(is_ai_primary=True)
        assert result.eligible is True
        assert result.rejection_codes == ()
        assert result.rejection_reasons == ()

    def test_eligible_is_never_computed_separately_from_rejection_codes(self) -> None:
        result = evaluate_eligibility(is_ai_primary=True, is_political=True)
        assert result.eligible == (not result.rejection_codes)


class TestMustReject:
    """One case per MUST REJECT example in the priority step's own test matrix."""

    def test_election_story_mentioning_ai_generated_campaign_content(self) -> None:
        # AI is a secondary angle on a fundamentally political story.
        result = evaluate_eligibility(is_ai_primary=False, is_political=True)
        assert result.eligible is False
        assert REJECT_POLITICS in result.rejection_codes
        assert REJECT_NOT_AI_FIRST in result.rejection_codes

    def test_politician_discussing_ai(self) -> None:
        result = evaluate_eligibility(is_ai_primary=False, is_political=True)
        assert result.eligible is False

    def test_military_drone_using_ai(self) -> None:
        result = evaluate_eligibility(is_ai_primary=False, is_miltech=True)
        assert result.eligible is False
        assert REJECT_MILTECH in result.rejection_codes

    def test_defence_startup_developing_ai_targeting_software(self) -> None:
        # A "startup" framing never exempts a real deftech story.
        result = evaluate_eligibility(is_ai_primary=False, is_deftech=True)
        assert result.eligible is False
        assert REJECT_DEFTECH in result.rejection_codes

    def test_battlefield_ai_surveillance_system(self) -> None:
        result = evaluate_eligibility(
            is_ai_primary=False, is_miltech=True, is_war_or_conflict=True
        )
        assert result.eligible is False
        assert REJECT_MILTECH in result.rejection_codes
        assert REJECT_WAR in result.rejection_codes

    def test_cyberattack_against_an_ai_company(self) -> None:
        # The target being an AI company does not make the incident itself AI-primary.
        result = evaluate_eligibility(is_ai_primary=False, is_cybersecurity=True)
        assert result.eligible is False
        assert REJECT_CYBERSECURITY in result.rejection_codes

    def test_malware_using_an_llm(self) -> None:
        result = evaluate_eligibility(is_ai_primary=False, is_cybersecurity=True)
        assert result.eligible is False

    def test_data_breach_at_an_ai_startup(self) -> None:
        result = evaluate_eligibility(is_ai_primary=False, is_cybersecurity=True)
        assert result.eligible is False

    def test_generic_cloud_platform_release_with_one_minor_ai_feature(self) -> None:
        result = evaluate_eligibility(is_ai_primary=False, is_generic_devtech=True)
        assert result.eligible is False
        assert REJECT_GENERIC_DEVTECH in result.rejection_codes

    def test_government_digitalisation_announcement_briefly_mentioning_ai(self) -> None:
        result = evaluate_eligibility(is_ai_primary=False, is_generic_government_news=True)
        assert result.eligible is False
        assert REJECT_GENERIC_GOVERNMENT in result.rejection_codes

    def test_political_ai_regulation_fight_whose_real_story_is_partisan_conflict(self) -> None:
        result = evaluate_eligibility(is_ai_primary=False, is_political=True)
        assert result.eligible is False

    def test_generic_doom_headline_about_ai_ending_humanity(self) -> None:
        result = evaluate_eligibility(is_ai_primary=True, is_speculative_doom=True)
        assert result.eligible is False
        assert REJECT_SPECULATIVE_DOOM in result.rejection_codes
        # AI genuinely is the subject here — this must not also fire REJECT_NOT_AI_FIRST.
        assert REJECT_NOT_AI_FIRST not in result.rejection_codes

    def test_story_primarily_about_russia_belarus_or_iran(self) -> None:
        result = evaluate_eligibility(is_ai_primary=True, is_about_forbidden_geography=True)
        assert result.eligible is False
        assert REJECT_FORBIDDEN_GEOGRAPHY in result.rejection_codes

    def test_ordinary_software_release_tagged_ai_powered_with_no_real_ai_capability(
        self,
    ) -> None:
        # The classifier is expected to answer is_ai_primary=False for this, not a
        # dedicated flag of its own — this is exactly the "removing AI would leave the
        # same story" case the core rule targets.
        result = evaluate_eligibility(is_ai_primary=False)
        assert result.eligible is False
        assert result.rejection_codes == (REJECT_NOT_AI_FIRST,)


class TestMustPass:
    """One case per MUST PASS example — all AI-primary, no hard filter fires."""

    @pytest.mark.parametrize(
        "scenario",
        [
            "new_model",
            "new_chatgpt_feature",
            "new_claude_feature",
            "new_gemini_capability",
            "new_video_generation_model",
            "new_image_generation_feature",
            "new_music_generation_capability",
            "practical_agent_workflow",
            "automation_tool_ai_central",
            "new_research_method",
            "coding_agent_release",
            "personal_ai_workflow",
        ],
    )
    def test_ai_primary_with_no_hard_filter_is_eligible(self, scenario: str) -> None:
        result = evaluate_eligibility(is_ai_primary=True)
        assert result.eligible is True, scenario


class TestEdgeCases:
    def test_ai_security_product_vs_cybersecurity_incident(self) -> None:
        # A genuinely new AI-native security product is AI-primary and not itself an
        # incident — passes. A security *incident* is not AI-primary even at an AI
        # company — rejected. The classifier, not this function, tells them apart.
        product = evaluate_eligibility(is_ai_primary=True, is_cybersecurity=False)
        incident = evaluate_eligibility(is_ai_primary=False, is_cybersecurity=True)
        assert product.eligible is True
        assert incident.eligible is False

    def test_ai_specific_regulation_vs_generic_political_story(self) -> None:
        ai_regulation = evaluate_eligibility(
            is_ai_primary=True, is_generic_government_news=False, is_political=False
        )
        generic_political = evaluate_eligibility(is_ai_primary=False, is_political=True)
        assert ai_regulation.eligible is True
        assert generic_political.eligible is False

    def test_ai_automation_vs_generic_saas_automation(self) -> None:
        ai_automation = evaluate_eligibility(is_ai_primary=True, is_generic_devtech=False)
        generic_saas = evaluate_eligibility(is_ai_primary=False, is_generic_devtech=True)
        assert ai_automation.eligible is True
        assert generic_saas.eligible is False

    def test_defence_adjacent_company_vs_actual_defence_technology(self) -> None:
        # An AI company that merely has defence-sector customers, with no deftech
        # angle to the story itself, is unaffected by this filter.
        adjacent = evaluate_eligibility(is_ai_primary=True, is_deftech=False)
        actual_deftech = evaluate_eligibility(is_ai_primary=False, is_deftech=True)
        assert adjacent.eligible is True
        assert actual_deftech.eligible is False

    def test_software_containing_ai_vs_an_ai_first_product(self) -> None:
        ai_first = evaluate_eligibility(is_ai_primary=True, is_generic_devtech=False)
        incidental_ai = evaluate_eligibility(is_ai_primary=False, is_generic_devtech=True)
        assert ai_first.eligible is True
        assert incidental_ai.eligible is False


class TestMultipleHardFiltersAreAllReported:
    def test_every_true_flag_produces_its_own_code_not_just_the_first(self) -> None:
        result = evaluate_eligibility(
            is_ai_primary=False,
            is_political=True,
            is_war_or_conflict=True,
            is_miltech=True,
            is_deftech=True,
            is_cybersecurity=True,
            is_generic_government_news=True,
            is_generic_devtech=True,
            is_speculative_doom=True,
            is_about_forbidden_geography=True,
        )
        assert result.eligible is False
        assert set(result.rejection_codes) == {
            REJECT_NOT_AI_FIRST,
            REJECT_POLITICS,
            REJECT_WAR,
            REJECT_MILTECH,
            REJECT_DEFTECH,
            REJECT_CYBERSECURITY,
            REJECT_GENERIC_GOVERNMENT,
            REJECT_GENERIC_DEVTECH,
            REJECT_SPECULATIVE_DOOM,
            REJECT_FORBIDDEN_GEOGRAPHY,
        }
        # Every code has a matching, non-empty human-readable reason, in the same order.
        assert len(result.rejection_reasons) == len(result.rejection_codes)
        assert all(reason for reason in result.rejection_reasons)


class TestFailClosedDefaults:
    def test_is_ai_primary_has_no_safe_default_every_other_flag_defaults_to_not_rejecting(
        self,
    ) -> None:
        """Every hard-filter flag defaults to False (does not fire) when omitted — the
        one exception is ``is_ai_primary`` itself, which has no default and must be
        answered explicitly by every caller (see the function signature)."""
        result = evaluate_eligibility(is_ai_primary=True)
        assert result.is_political is False
        assert result.is_war_or_conflict is False
        assert result.is_miltech is False
        assert result.is_deftech is False
        assert result.is_cybersecurity is False
        assert result.is_generic_government_news is False
        assert result.is_generic_devtech is False
        assert result.is_speculative_doom is False
        assert result.is_about_forbidden_geography is False

    def test_is_ai_primary_false_alone_is_sufficient_to_reject(self) -> None:
        """The fail-closed case section 5 requires: a classifier that could not
        confidently determine AI relevance must reject, not publish."""
        result = evaluate_eligibility(is_ai_primary=False)
        assert result.eligible is False
        assert result.rejection_codes == (REJECT_NOT_AI_FIRST,)

    def test_evaluate_eligibility_never_raises_for_any_boolean_combination(self) -> None:
        """Deterministic and total: every one of the 2**10 combinations must return a
        result, never throw — spot-checked across a representative sample rather than
        exhaustively enumerated."""
        flag_names = [
            "is_ai_primary", "is_political", "is_war_or_conflict", "is_miltech",
            "is_deftech", "is_cybersecurity", "is_generic_government_news",
            "is_generic_devtech", "is_speculative_doom", "is_about_forbidden_geography",
        ]
        for combo in itertools.product([True, False], repeat=len(flag_names)):
            kwargs = dict(zip(flag_names, combo, strict=True))
            result = evaluate_eligibility(**kwargs)
            assert isinstance(result.eligible, bool)
