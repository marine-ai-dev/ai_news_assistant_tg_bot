"""Content-policy checks that sit in front of approval and publication.

The approval gate answers one question: *did a human approve this exact version?* It
deliberately knows nothing about content policy, and it should stay that way — a gate
that grows editorial rules is a gate that eventually has an exception in it.

This module holds the rules that are about the content rather than the approval, and the
gate calls it. Right now there is exactly one:

    **A prompt post must rest on a demonstration somebody published.**

Phase 7.5 let prompts be editorial-original, which in practice meant inventing something
plausible and presenting it as advice. Documentation alone would not fix that: the next
batch would be written by a session that had not read the style guide, and nothing would
stop it. So the rule is enforced where publication is decided, and a human approving the
draft does not override it — approval says "this reads well and I want it out", which is
not the same claim as "somebody demonstrated this".
"""

from __future__ import annotations

import sqlite3

from ai_news_editor.domain.enums import ContentType, EvidenceStatus
from ai_news_editor.domain.models import Draft
from ai_news_editor.storage.repositories import ContentItemRepository


class NotPublishableError(Exception):
    """Content that a policy rule refuses to publish, whatever its approval says."""


def publication_problem(connection: sqlite3.Connection, draft: Draft) -> str | None:
    """Why this draft may not be published, or ``None`` if policy permits it.

    Only prompts are constrained. News carries an article and an evaluation; an explainer
    is editorial-original by design and says so. A prompt claims that something worked
    for somebody, and that claim needs a source.
    """
    if draft.content_type is not ContentType.PROMPT:
        return None
    if draft.content_item_id is None:  # pragma: no cover - the model forbids this
        return "this prompt has no content item, so its provenance cannot be checked"

    item = ContentItemRepository(connection).get(draft.content_item_id)

    if item.evidence_status is EvidenceStatus.VERIFIED_SOURCE_BACKED:
        if item.evidence is None:  # pragma: no cover - the model forbids this
            return "this prompt is marked source-backed but carries no evidence"
        return None

    if item.evidence_status is EvidenceStatus.LEGACY_UNVERIFIED:
        return (
            "this prompt was written before prompts were required to rest on a published "
            "demonstration, so there is no source to point a reader at. It cannot be "
            "published. Finding a real tested workflow and writing a new post is the "
            "way forward — inventing a source for this one is not."
        )

    return (
        "this prompt's source did not actually demonstrate the workflow, so it is not "
        "publishable"
    )


def assert_publishable(connection: sqlite3.Connection, draft: Draft) -> None:
    """Raise if content policy refuses this draft.

    Raises:
        NotPublishableError: with a reason written for the person reading it.
    """
    problem = publication_problem(connection, draft)
    if problem is not None:
        raise NotPublishableError(problem)
