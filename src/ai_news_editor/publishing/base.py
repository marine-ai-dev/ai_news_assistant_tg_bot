"""The publishing boundary.

No publisher exists yet — Telegram arrives in Phase 7. What exists is the *shape* a
publisher must have, and it is deliberately hostile to accidents:

    def publish(self, version: DraftVersion, authorization: PublishAuthorization) -> ...

``PublishAuthorization`` can only be produced by :func:`publishing.gate.approve_draft`,
so "publish without approval" is not an error a future implementer must remember to
check. It is a call they cannot write, because they would have nothing to pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ai_news_editor.domain.authorization import PublishAuthorization
from ai_news_editor.domain.clock import UtcDatetime
from ai_news_editor.domain.models import DraftVersion


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """Proof that a publisher accepted a post.

    Deliberately transport-agnostic: Phase 7's Telegram publisher will fill
    ``external_id`` with a message id, but nothing above this layer needs to know that.
    """

    draft_id: object
    draft_version_id: object
    external_id: str | None = None
    target: str | None = None
    published_at: UtcDatetime | None = None


@runtime_checkable
class Publisher(Protocol):
    """Sends an approved draft version somewhere.

    The authorization argument is not decoration. It is the only way to express that a
    human approved this exact version, and every implementation must pass it through
    :func:`publishing.gate.publish_with_gate` rather than trusting it on sight.
    """

    name: str

    def publish(
        self, version: DraftVersion, authorization: PublishAuthorization
    ) -> PublicationReceipt:
        """Send ``version``. Called only after the gate has verified the authorization."""
        ...
