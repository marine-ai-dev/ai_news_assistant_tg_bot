"""editorial.policy — Step 3 sections 13-14."""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import EditorialCategory
from ai_news_editor.editorial.policy import CATEGORY_PROMPTS, SHARED_SAFETY_RULES, prompt_for


class TestEveryCategoryHasAPrompt:
    @pytest.mark.parametrize("category", list(EditorialCategory))
    def test_prompt_for_returns_a_contract_for_every_category(
        self, category: EditorialCategory
    ) -> None:
        contract = prompt_for(category)
        assert contract.category == category
        assert contract.purpose
        assert contract.specific_rules
        assert contract.allowed_evidence

    @pytest.mark.parametrize("category", list(EditorialCategory))
    def test_the_rendered_instruction_includes_the_shared_safety_rules(
        self, category: EditorialCategory
    ) -> None:
        rendered = prompt_for(category).render_instruction()
        assert SHARED_SAFETY_RULES in rendered

    def test_the_table_covers_every_editorial_category_exactly_once(self) -> None:
        assert set(CATEGORY_PROMPTS.keys()) == set(EditorialCategory)


class TestLifehackNeverUpgradesAnecdoteToFact:
    def test_the_rules_forbid_stating_a_report_as_established_fact(self) -> None:
        rendered = prompt_for(EditorialCategory.AI_LIFEHACK).render_instruction()
        assert "never as a flat factual claim" in rendered
        assert "reported anecdote, not a verified fact" in rendered
        assert "never upgrade" in rendered.lower()

    def test_lifehack_evidence_is_limited_to_user_reported_and_community(self) -> None:
        from ai_news_editor.domain.enums import EditorialEvidence

        allowed = prompt_for(EditorialCategory.AI_LIFEHACK).allowed_evidence
        assert set(allowed) == {
            EditorialEvidence.USER_REPORTED,
            EditorialEvidence.COMMUNITY_DISCUSSION,
        }


class TestPromptWorkflowTracksProvenance:
    def test_the_rules_mention_verbatim_adapted_and_derived(self) -> None:
        rendered = prompt_for(EditorialCategory.PROMPT_WORKFLOW).render_instruction()
        assert "verbatim" in rendered
        assert "adapted" in rendered
        assert "derived" in rendered

    def test_the_rules_forbid_quoting_non_verbatim_text(self) -> None:
        rendered = prompt_for(EditorialCategory.PROMPT_WORKFLOW).render_instruction()
        assert "quotation marks" in rendered


class TestFreeDealFailsClosed:
    def test_the_rules_require_explicit_evidence_before_publishing(self) -> None:
        rendered = prompt_for(EditorialCategory.FREE_DEAL).render_instruction()
        assert "explicitly state" in rendered
        assert "fails closed" in rendered.lower() or "fail closed" in rendered.lower()


class TestResearchDistinguishesClaimStrength:
    def test_the_rules_separate_paper_result_from_company_claim_and_verification(self) -> None:
        rendered = prompt_for(EditorialCategory.RESEARCH).render_instruction()
        assert "paper" in rendered.lower()
        assert "company" in rendered.lower() or "press materials" in rendered.lower()
        assert "independent" in rendered.lower()


class TestWeeklyDigestDoesNotReclassify:
    def test_the_rules_forbid_adding_new_claims_or_reclassifying_items(self) -> None:
        rendered = prompt_for(EditorialCategory.WEEKLY_DIGEST).render_instruction()
        assert "does not re-classify" in rendered or "not re-classify" in rendered
