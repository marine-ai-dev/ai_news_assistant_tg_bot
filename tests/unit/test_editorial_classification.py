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
