"""editorial.safety — Step 3 sections 15-18."""

from __future__ import annotations

import pytest

from ai_news_editor.domain.enums import EditorialEvidence, PromptOrigin
from ai_news_editor.editorial.safety import (
    EditorialSafetyError,
    FreeDealClaim,
    LifehackClaim,
    PromptClaim,
    ResearchClaim,
    ResearchClaimFraming,
    validate_free_deal,
    validate_lifehack,
    validate_prompt_provenance,
    validate_research_claim,
)


class TestLifehackSafety:
    def test_a_properly_framed_user_report_is_allowed(self) -> None:
        validate_lifehack(
            LifehackClaim(evidence=EditorialEvidence.USER_REPORTED, framed_as_report=True)
        )

    def test_stating_the_claim_as_established_fact_is_rejected(self) -> None:
        """The exact failure this category exists to prevent: 'user reported X'
        silently becoming 'AI does X'."""
        with pytest.raises(EditorialSafetyError, match="never as an established fact"):
            validate_lifehack(
                LifehackClaim(evidence=EditorialEvidence.USER_REPORTED, framed_as_report=False)
            )

    def test_primary_source_evidence_is_rejected_even_if_framed_correctly(self) -> None:
        """A lifehack cannot borrow PRIMARY_SOURCE-level certainty just because the
        post itself is worded carefully — the evidence tier is checked independently."""
        with pytest.raises(EditorialSafetyError, match="USER_REPORTED or COMMUNITY_DISCUSSION"):
            validate_lifehack(
                LifehackClaim(evidence=EditorialEvidence.PRIMARY_SOURCE, framed_as_report=True)
            )

    def test_community_discussion_evidence_is_allowed(self) -> None:
        validate_lifehack(
            LifehackClaim(
                evidence=EditorialEvidence.COMMUNITY_DISCUSSION, framed_as_report=True
            )
        )


class TestPromptProvenance:
    def test_verbatim_text_may_be_quoted(self) -> None:
        validate_prompt_provenance(
            PromptClaim(origin=PromptOrigin.SOURCE_VERBATIM, presented_as_verbatim_quote=True)
        )

    def test_adapted_text_may_not_be_quoted_as_verbatim(self) -> None:
        with pytest.raises(EditorialSafetyError, match="SOURCE_ADAPTED"):
            validate_prompt_provenance(
                PromptClaim(
                    origin=PromptOrigin.SOURCE_ADAPTED, presented_as_verbatim_quote=True
                )
            )

    def test_derived_text_may_not_be_quoted_as_verbatim(self) -> None:
        with pytest.raises(EditorialSafetyError, match="WORKFLOW_DERIVED"):
            validate_prompt_provenance(
                PromptClaim(
                    origin=PromptOrigin.WORKFLOW_DERIVED, presented_as_verbatim_quote=True
                )
            )

    def test_adapted_text_not_presented_as_a_quote_is_fine(self) -> None:
        validate_prompt_provenance(
            PromptClaim(origin=PromptOrigin.SOURCE_ADAPTED, presented_as_verbatim_quote=False)
        )


class TestFreeDealFailsClosed:
    def test_explicit_evidence_is_required_and_sufficient(self) -> None:
        validate_free_deal(FreeDealClaim(has_explicit_free_evidence=True))

    def test_no_evidence_is_rejected_rather_than_assumed(self) -> None:
        with pytest.raises(EditorialSafetyError, match="failing closed"):
            validate_free_deal(FreeDealClaim(has_explicit_free_evidence=False))


class TestResearchClaimFraming:
    def test_a_paper_result_needs_no_independent_verification(self) -> None:
        validate_research_claim(
            ResearchClaim(
                framing=ResearchClaimFraming.PAPER_RESULT, independently_verified=False
            )
        )

    def test_a_company_claim_needs_no_independent_verification(self) -> None:
        validate_research_claim(
            ResearchClaim(
                framing=ResearchClaimFraming.COMPANY_CLAIM, independently_verified=False
            )
        )

    def test_independent_verification_framing_requires_it_to_be_true(self) -> None:
        with pytest.raises(EditorialSafetyError, match="independent verification"):
            validate_research_claim(
                ResearchClaim(
                    framing=ResearchClaimFraming.INDEPENDENT_VERIFICATION,
                    independently_verified=False,
                )
            )

    def test_independent_verification_framing_is_allowed_when_actually_verified(self) -> None:
        validate_research_claim(
            ResearchClaim(
                framing=ResearchClaimFraming.INDEPENDENT_VERIFICATION,
                independently_verified=True,
            )
        )
