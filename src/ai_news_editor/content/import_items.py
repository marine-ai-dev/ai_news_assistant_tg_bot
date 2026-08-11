"""Validating and importing an editorial-content batch.

Same discipline as the editorial and writing imports: the whole batch is validated
before anything is written, so a malformed item stops the import while the database is
still untouched. Re-importing the same file adds nothing.

Each accepted item produces a ``ContentItem`` (the editorial substance) and a ``Draft``
+ ``DraftVersion`` in ``PENDING_REVIEW`` (the post). Like the writing import, items are
written one at a time rather than under a single transaction — the repositories manage
their own — so the guarantee is "nothing invalid is written", not "all or nothing under
a disk failure". Re-running the import after such a failure resumes cleanly, because
the skip rule is idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from ai_news_editor.content.jargon import unexplained
from ai_news_editor.content.schema import (
    ContentBatch,
    SubmittedExplainer,
    SubmittedItem,
    SubmittedPrompt,
)
from ai_news_editor.domain.enums import NON_TECHNICAL_AUDIENCES, ContentType, DraftStatus
from ai_news_editor.domain.errors import AiNewsError
from ai_news_editor.domain.models import (
    ContentItem,
    ContentReference,
    ExplainerBody,
    PromptBody,
)
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.storage.repositories import ContentItemRepository, DraftRepository
from ai_news_editor.writing.format import render_post
from ai_news_editor.writing.schema import STYLE_VERSION

logger = get_logger(__name__)

#: Editorial-original content carries no source line. Stored so the column is honest
#: about why rather than empty: this material was written here.
EDITORIAL_ATTRIBUTION = "Матеріал каналу"


class ContentImportError(AiNewsError):
    """A content batch could not be validated or imported."""


@dataclass
class ImportOutcome:
    """What an import did, and what a reviewer should look at."""

    created: list[tuple[UUID, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: Jargon a NEWCOMER or BEGINNER post uses without an apparent explanation. A note
    #: for the reviewer, never a reason to refuse the import.
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.created)


def load_batch(path: Path) -> ContentBatch:
    """Parse and validate a batch file without writing anything.

    Raises:
        ContentImportError: the file is missing, not JSON, or fails validation.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentImportError(f"no such file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContentImportError(f"{path.name} is not valid JSON: {exc}") from exc

    try:
        return ContentBatch.model_validate(raw)
    except ValidationError as exc:
        raise ContentImportError(_readable(exc, path.name)) from exc


def review_notes(batch: ContentBatch) -> list[str]:
    """Jargon warnings for the items in a batch, for a reviewer's attention.

    Only for content aimed at readers with no technical background. A TECH_CURIOUS post
    is allowed to say "context window" without stopping to define it.
    """
    notes: list[str] = []
    for item in batch.items:
        if item.audience not in NON_TECHNICAL_AUDIENCES:
            continue
        flagged = unexplained(f"{item.post.headline}\n{item.post.body}")
        for note in flagged:
            notes.append(f"{item.title}: {note.message}")
    return notes


def import_batch(connection: sqlite3.Connection, path: Path) -> ImportOutcome:
    """Import a validated batch atomically.

    Raises:
        ContentImportError: validation failed. Nothing is written.
    """
    batch = load_batch(path)
    items = ContentItemRepository(connection)
    drafts = DraftRepository(connection)

    outcome = ImportOutcome(warnings=review_notes(batch))

    for submitted in batch.items:
        existing = items.find_by_title(submitted.content_type, submitted.title)
        if existing is not None:
            outcome.skipped.append(submitted.title)
            continue

        stored = items.add(_to_content_item(submitted, author=batch.author))
        draft, _version = drafts.create(
            content_item_id=stored.id,
            content_type=stored.content_type,
            title=submitted.post.headline,
            body=submitted.post.body,
            category=submitted.post.category,
            audience=submitted.audience,
            source_attribution=EDITORIAL_ATTRIBUTION,
            source_url=None,
            post_format=submitted.post.post_format,
            style_version=batch.style_version,
            hashtags=submitted.post.hashtags,
            writer_notes=submitted.post.writer_notes,
            created_by=f"claude-code:content_v{STYLE_VERSION}",
        )
        # Straight to review, like every other draft. There is no other option here
        # and no parameter that could ask for one.
        drafts.set_status(draft.id, DraftStatus.PENDING_REVIEW)
        outcome.created.append((draft.id, submitted.title))

    logger.info(
        "content batch imported",
        extra={
            "batch_id": batch.batch_id,
            "drafts_created": outcome.count,
            "skipped": len(outcome.skipped),
        },
    )
    return outcome


def _to_content_item(submitted: SubmittedItem, *, author: str) -> ContentItem:
    references = tuple(
        ContentReference(label=r.label, url=r.url, supports=r.supports)
        for r in submitted.references
    )
    if isinstance(submitted, SubmittedPrompt):
        return ContentItem(
            content_type=ContentType.PROMPT,
            audience=submitted.audience,
            title=submitted.title,
            topic=submitted.topic,
            body=PromptBody(
                what_you_can_do=submitted.what_you_can_do,
                prompt_text=submitted.prompt_text,
                customization_tips=tuple(submitted.customization_tips),
                works_with=submitted.works_with,
            ),
            references=references,
            created_by=author,
        )
    if isinstance(submitted, SubmittedExplainer):
        return ContentItem(
            content_type=ContentType.EXPLAINER,
            audience=submitted.audience,
            title=submitted.title,
            body=ExplainerBody(
                concept=submitted.concept,
                simple_explanation=submitted.simple_explanation,
                real_life_example=submitted.real_life_example,
                why_it_matters=submitted.why_it_matters,
                try_this=submitted.try_this,
            ),
            references=references,
            created_by=author,
        )
    raise ContentImportError(  # pragma: no cover - the union has no third member
        f"unsupported content type {submitted!r}"
    )


def rendered_preview(submitted: SubmittedItem) -> str:
    """The post as it would appear, for `content validate` output."""
    return render_post(headline=submitted.post.headline, body=submitted.post.body)


def _readable(exc: ValidationError, filename: str) -> str:
    lines = [f"{filename} is not a valid content batch:"]
    for error in exc.errors()[:12]:
        where = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  {where}: {error['msg']}")
    return "\n".join(lines)
