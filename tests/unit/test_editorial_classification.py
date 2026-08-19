"""automation.classification — Step 3 section 12.

A standalone mock-transport harness, deliberately separate from test_automation.py's
full pipeline fixtures: this module is not wired into the live pipeline, so its tests
do not need that machinery either.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai_news_editor.automation.classification import (
    ClassificationInvalid,
    ClassificationRejected,
    classify_candidate,
)
from ai_news_editor.automation.gemini import GeminiClient
from ai_news_editor.automation.schema import SelectionCandidate
from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence

FAKE_KEY = "fake-test-key"


def _transport(payload: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": json.dumps(payload)}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


def _candidate(**overrides: object) -> SelectionCandidate:
    data: dict[str, object] = {
        "id": "1",
        "source_name": "OpenAI Blog",
        "title": "OpenAI announces a new tool",
        "url": "https://openai.com/blog/example",
        "summary": "A short excerpt describing the new tool.",
    }
    data.update(overrides)
    return SelectionCandidate.model_validate(data)


class TestClassifyCandidate:
    def test_a_valid_classification_is_returned(self) -> None:
        payload = {
            "content_type": "AI_TOOL",
            "evidence_type": "OFFICIAL_PRODUCT_PAGE",
            "reason": "Official vendor announcement of a new product.",
            "rejection_reason": None,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))
        classification = classify_candidate(client, _candidate())

        assert classification.content_type == EditorialCategory.AI_TOOL
        assert classification.evidence_type == EditorialEvidence.OFFICIAL_PRODUCT_PAGE
        assert classification.reason == "Official vendor announcement of a new product."

    def test_a_rejection_raises_classification_rejected(self) -> None:
        payload = {
            "content_type": None,
            "evidence_type": None,
            "reason": None,
            "rejection_reason": "Not enough information to classify confidently.",
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        with pytest.raises(ClassificationRejected, match="Not enough information"):
            classify_candidate(client, _candidate())

    def test_an_invented_content_type_is_rejected_by_the_schema(self) -> None:
        """Gemini's own response schema constrains the enum wire-side; this proves the
        Python-side re-validation catches anything that slipped through anyway (a
        transport-level swap, a hand-crafted test payload) rather than trusting the
        schema alone."""
        payload = {
            "content_type": "BREAKING_EXCLUSIVE",
            "evidence_type": "OFFICIAL_PRODUCT_PAGE",
            "reason": None,
            "rejection_reason": None,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        with pytest.raises(ClassificationInvalid, match="did not match the schema"):
            classify_candidate(client, _candidate())

    def test_malformed_json_is_classification_invalid(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "not json"}]}, "finishReason": "STOP"}
                    ]
                },
            )

        client = GeminiClient(
            FAKE_KEY, model="gemini-test", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ClassificationInvalid, match="unusable"):
            classify_candidate(client, _candidate())

    def test_content_type_without_evidence_type_is_invalid(self) -> None:
        """The schema requires both fields together, or neither — never one alone."""
        payload = {
            "content_type": "NEWS",
            "evidence_type": None,
            "reason": None,
            "rejection_reason": None,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        with pytest.raises(ClassificationInvalid):
            classify_candidate(client, _candidate())


class TestSpeculativeDoomExclusion:
    """Step 6B: rejected inside this same classification call, deterministically —
    never a second Gemini call, never left to Gemini's own rejection_reason wording."""

    def test_a_jill_lepore_style_artificial_state_story_is_rejected(self) -> None:
        """Modeled on the spec's own example: a hypothetical essay about AI quietly
        assuming the functions of government — speculative futurism, not a report of
        anything that has actually happened."""
        payload = {
            "content_type": "EXPLAINER",
            "evidence_type": "PRIMARY_SOURCE",
            "reason": "Speculative essay about an 'artificial state' run by AI systems.",
            "rejection_reason": None,
            "is_speculative_doom": True,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        with pytest.raises(ClassificationRejected, match="speculative dystopian"):
            classify_candidate(
                client,
                _candidate(
                    title="What Happens When AI Quietly Becomes the State",
                    summary=(
                        "An essay imagining a future where algorithmic systems have "
                        "gradually taken over the functions of government."
                    ),
                ),
            )

    def test_is_speculative_doom_overrides_an_otherwise_valid_classification(self) -> None:
        """True is unconditional — never overridden by a content_type Gemini also
        filled in on the same response."""
        payload = {
            "content_type": "NEWS",
            "evidence_type": "PRIMARY_SOURCE",
            "reason": "Looked classifiable, but flagged as doom speculation.",
            "rejection_reason": None,
            "is_speculative_doom": True,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        with pytest.raises(ClassificationRejected):
            classify_candidate(client, _candidate())

    def test_concrete_present_day_reporting_is_not_excluded(self) -> None:
        """A real security incident is serious, but it is not speculation — it must
        classify normally, not get swept up by the doom filter."""
        payload = {
            "content_type": "NEWS",
            "evidence_type": "PRIMARY_SOURCE",
            "reason": "Reports a documented data breach and the regulator's response.",
            "rejection_reason": None,
            "is_speculative_doom": False,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        classification = classify_candidate(
            client,
            _candidate(
                title="Regulator fines company after AI system exposed user data",
                summary=(
                    "A data protection authority issued a fine after an AI-powered "
                    "feature leaked personal records; the company confirmed the "
                    "incident and its response."
                ),
            ),
        )
        assert classification.content_type == EditorialCategory.NEWS

    def test_is_speculative_doom_defaults_to_false_when_omitted(self) -> None:
        """Every existing test payload above omits this key entirely — proving the
        new field is additive and does not break a caller who does not send it."""
        payload = {
            "content_type": "AI_TOOL",
            "evidence_type": "OFFICIAL_PRODUCT_PAGE",
            "reason": "Official vendor announcement.",
            "rejection_reason": None,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        classification = classify_candidate(client, _candidate())
        assert classification.content_type == EditorialCategory.AI_TOOL


class TestForbiddenGeographyExclusion:
    """Step 6C: a hard content filter — a story substantially ABOUT Russia, Belarus,
    or Iran is rejected regardless of which source reported it, deterministically,
    inside this same classification call (no extra Gemini call)."""

    @pytest.mark.parametrize("country", ["Russia", "Belarus", "Iran"])
    def test_a_story_about_a_forbidden_country_is_rejected(self, country: str) -> None:
        payload = {
            "content_type": "NEWS",
            "evidence_type": "REPUTABLE_SECONDARY",
            "reason": f"Story is about an AI lab in {country}.",
            "rejection_reason": None,
            "is_speculative_doom": False,
            "is_about_forbidden_geography": True,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        with pytest.raises(ClassificationRejected, match="Russia, Belarus, or Iran"):
            classify_candidate(
                client,
                _candidate(
                    title=f"{country}'s new AI lab unveils a research model",
                    summary=f"A state-linked AI research lab in {country} announced a new model.",
                ),
            )

    def test_is_about_forbidden_geography_overrides_an_otherwise_valid_classification(
        self,
    ) -> None:
        """True is unconditional — never overridden by a content_type Gemini also
        filled in on the same response, matching is_speculative_doom's own discipline."""
        payload = {
            "content_type": "NEWS",
            "evidence_type": "PRIMARY_SOURCE",
            "reason": "Looked classifiable, but flagged as forbidden-geography focus.",
            "rejection_reason": None,
            "is_speculative_doom": False,
            "is_about_forbidden_geography": True,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        with pytest.raises(ClassificationRejected):
            classify_candidate(client, _candidate())

    def test_a_us_outlet_reporting_on_a_forbidden_country_is_still_rejected(self) -> None:
        """The rule is about the story's own subject, not the source's origin — even
        a US/EU/UK/UA outlet's story about Russia's AI sector is rejected."""
        payload = {
            "content_type": "NEWS",
            "evidence_type": "REPUTABLE_SECONDARY",
            "reason": "TechCrunch reporting on Russian state AI investment.",
            "rejection_reason": None,
            "is_speculative_doom": False,
            "is_about_forbidden_geography": True,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        with pytest.raises(ClassificationRejected, match="Russia, Belarus, or Iran"):
            classify_candidate(
                client,
                _candidate(
                    source_name="TechCrunch",
                    title="Russia pours billions into state AI research",
                    summary="Western outlet reporting on Russia's national AI investment plan.",
                ),
            )

    def test_a_story_merely_mentioning_a_forbidden_country_in_passing_is_not_excluded(
        self,
    ) -> None:
        """A story that is not substantially about one of the three countries must
        classify normally — this is a focus check, not a keyword ban."""
        payload = {
            "content_type": "NEWS",
            "evidence_type": "PRIMARY_SOURCE",
            "reason": "US sanctions story that briefly names Russia as context.",
            "rejection_reason": None,
            "is_speculative_doom": False,
            "is_about_forbidden_geography": False,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        classification = classify_candidate(
            client,
            _candidate(
                title="US tightens export controls on advanced AI chips",
                summary=(
                    "The US government expanded chip export restrictions, extending "
                    "existing controls that also cover Russia among other countries."
                ),
            ),
        )
        assert classification.content_type == EditorialCategory.NEWS

    def test_is_about_forbidden_geography_defaults_to_false_when_omitted(self) -> None:
        """Additive field — a payload that omits it entirely must not break."""
        payload = {
            "content_type": "AI_TOOL",
            "evidence_type": "OFFICIAL_PRODUCT_PAGE",
            "reason": "Official vendor announcement.",
            "rejection_reason": None,
        }
        client = GeminiClient(FAKE_KEY, model="gemini-test", transport=_transport(payload))

        classification = classify_candidate(client, _candidate())
        assert classification.content_type == EditorialCategory.AI_TOOL
