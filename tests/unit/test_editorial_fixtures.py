"""tests/fixtures/editorial/sample_candidates.json — Step 3 section 34.

Checks the fixture stays internally consistent with the real enums as they evolve,
rather than silently drifting into an example that no longer type-checks.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_news_editor.domain.enums import EditorialCategory, EditorialEvidence, TrustTier

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "editorial" / "sample_candidates.json"
)


def _load() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestSampleCandidatesFixture:
    def test_the_fixture_is_explicitly_labeled(self) -> None:
        data = _load()
        assert data["_fixture"] is True
        assert isinstance(data["_purpose"], str) and data["_purpose"]

    def test_every_editorial_category_has_exactly_one_sample(self) -> None:
        data = _load()
        content_types = [sample["content_type"] for sample in data["samples"]]
        assert sorted(content_types) == sorted(member.value for member in EditorialCategory)

    def test_every_sample_content_type_is_a_real_editorial_category(self) -> None:
        data = _load()
        for sample in data["samples"]:
            EditorialCategory(sample["content_type"])

    def test_every_non_null_evidence_type_is_real(self) -> None:
        data = _load()
        for sample in data["samples"]:
            if sample["evidence_type"] is not None:
                EditorialEvidence(sample["evidence_type"])

    def test_every_non_null_trust_tier_is_real(self) -> None:
        data = _load()
        for sample in data["samples"]:
            if sample["trust_tier"] is not None:
                TrustTier(sample["trust_tier"])

    def test_every_sample_has_a_note_explaining_its_evidence(self) -> None:
        data = _load()
        for sample in data["samples"]:
            assert sample["note"].strip()
