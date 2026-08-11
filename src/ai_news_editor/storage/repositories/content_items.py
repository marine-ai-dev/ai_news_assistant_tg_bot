"""Persistence for editorial-original content — prompts and explainers.

The counterpart to :class:`ArticleRepository`: where that stores what somebody else
published, this stores what this newsroom wrote. Keeping them in separate tables is the
point. A prompt has no publisher, no publication date and no URL, and giving it a row
in ``articles`` would make it indistinguishable from something that was reported.
"""

from __future__ import annotations

import json
import sqlite3
from uuid import UUID

from ai_news_editor.domain.clock import to_iso
from ai_news_editor.domain.enums import ContentType
from ai_news_editor.domain.errors import EntityNotFoundError
from ai_news_editor.domain.models import ContentItem, ExplainerBody, PromptBody


def _to_domain(row: sqlite3.Row) -> ContentItem:
    data = dict(row)
    payload = json.loads(data.pop("payload_json"))
    references = json.loads(data.pop("references_json"))
    body: PromptBody | ExplainerBody = (
        PromptBody.model_validate(payload)
        if data["content_type"] == ContentType.PROMPT.value
        else ExplainerBody.model_validate(payload)
    )
    return ContentItem.model_validate({**data, "body": body, "references": references})


class ContentItemRepository:
    """Reads and writes ``content_items``."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add(self, item: ContentItem) -> ContentItem:
        """Store one item. Returns the existing row if this title was already imported.

        Idempotent on ``(content_type, title)`` so re-running an import adds nothing —
        the same rule the editorial and writing imports already follow.
        """
        existing = self.find_by_title(item.content_type, item.title)
        if existing is not None:
            return existing

        self._conn.execute(
            """
            INSERT INTO content_items (id, content_type, origin, audience, title, topic,
                                       payload_json, references_json, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.id),
                item.content_type.value,
                item.origin.value,
                item.audience.value,
                item.title,
                item.topic.value if item.topic else None,
                json.dumps(item.body.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(
                    [r.model_dump(mode="json") for r in item.references], ensure_ascii=False
                ),
                item.created_by,
                to_iso(item.created_at),
            ),
        )
        return item

    def get(self, item_id: UUID) -> ContentItem:
        row = self._conn.execute(
            "SELECT * FROM content_items WHERE id = ?", (str(item_id),)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"content item {item_id} not found")
        return _to_domain(row)

    def find_by_title(self, content_type: ContentType, title: str) -> ContentItem | None:
        row = self._conn.execute(
            "SELECT * FROM content_items WHERE content_type = ? AND title = ?",
            (content_type.value, title),
        ).fetchone()
        return _to_domain(row) if row else None

    def list_by_type(
        self, content_type: ContentType | None = None, *, limit: int = 100
    ) -> list[ContentItem]:
        if content_type is None:
            rows = self._conn.execute(
                "SELECT * FROM content_items ORDER BY created_at DESC, id LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM content_items WHERE content_type = ? "
                "ORDER BY created_at DESC, id LIMIT ?",
                (content_type.value, limit),
            ).fetchall()
        return [_to_domain(row) for row in rows]

    def without_draft(self, content_type: ContentType | None = None) -> list[ContentItem]:
        """Items that have not been written up yet.

        The writing step is separate from the idea step, so this is how the exporter
        finds what still needs a Ukrainian post.
        """
        sql = (
            "SELECT c.* FROM content_items c "
            "LEFT JOIN drafts d ON d.content_item_id = c.id "
            "WHERE d.id IS NULL"
        )
        params: tuple[str, ...] = ()
        if content_type is not None:
            sql += " AND c.content_type = ?"
            params = (content_type.value,)
        rows = self._conn.execute(sql + " ORDER BY c.created_at", params).fetchall()
        return [_to_domain(row) for row in rows]

    def count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) AS n FROM content_items").fetchone()["n"]
        )
