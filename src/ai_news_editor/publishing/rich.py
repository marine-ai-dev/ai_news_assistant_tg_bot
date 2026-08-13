"""Publishing a rich bundle: several Telegram calls, recorded one at a time.

Phase 7 published one message and recorded one row. That was honest for text. A bundle
is an image, the post, a comment and a file, and Telegram offers no transaction across
them — so this module does the only thing that is actually safe:

1. build the whole plan first, checking every file, so a bundle fails whole rather than
   arriving in pieces;
2. read what already exists for this exact version, so a resume never repeats a send;
3. execute the remaining steps in order, recording each one as it finishes;
4. stop at the first uncertain outcome rather than guessing.

**The main message is never sent twice.** That is the rule everything else serves. A
comment that failed is an annoyance; a second copy of the post on the channel is
something a reader sees and the owner cannot undo.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from ai_news_editor.domain.clock import now_utc, to_iso
from ai_news_editor.domain.enums import PublicationStatus
from ai_news_editor.domain.errors import PublicationOutcomeUncertainError, TelegramError
from ai_news_editor.domain.models import DraftVersion
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.observability.redaction import redact
from ai_news_editor.publishing.plan import BundlePlan, Component, Step, check_asset
from ai_news_editor.publishing.telegram import TelegramClient

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComponentOutcome:
    """What happened to one part of a bundle."""

    component: Component
    method: str
    status: PublicationStatus | str
    message_id: int | None = None
    chat_id: str | None = None
    failure_reason: str | None = None


class ComponentRepository:
    """Append-only record of what each part of a publication did."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def add(
        self,
        *,
        publication_id: UUID,
        draft_id: UUID,
        draft_version_id: UUID,
        outcome: ComponentOutcome,
    ) -> None:
        status = (
            outcome.status.value
            if isinstance(outcome.status, PublicationStatus)
            else str(outcome.status)
        )
        self._conn.execute(
            """
            INSERT INTO publication_components (id, publication_id, draft_id,
                                                draft_version_id, component, method,
                                                status, message_id, chat_id,
                                                failure_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                str(publication_id),
                str(draft_id),
                str(draft_version_id),
                outcome.component.value,
                outcome.method,
                status,
                outcome.message_id,
                outcome.chat_id,
                redact(outcome.failure_reason) if outcome.failure_reason else None,
                to_iso(now_utc()),
            ),
        )

    def succeeded(self, draft_version_id: UUID) -> set[Component]:
        """Components already confirmed on the channel for this exact version."""
        rows = self._conn.execute(
            "SELECT DISTINCT component FROM publication_components "
            "WHERE draft_version_id = ? AND status = 'SUCCEEDED'",
            (str(draft_version_id),),
        ).fetchall()
        return {Component(row["component"]) for row in rows}

    def uncertain(self, draft_version_id: UUID) -> set[Component]:
        """Components that may or may not exist. Never retried automatically."""
        rows = self._conn.execute(
            "SELECT DISTINCT component FROM publication_components "
            "WHERE draft_version_id = ? AND status = 'UNCERTAIN'",
            (str(draft_version_id),),
        ).fetchall()
        return {Component(row["component"]) for row in rows}

    def for_publication(self, publication_id: UUID) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM publication_components WHERE publication_id = ? "
            "ORDER BY created_at, id",
            (str(publication_id),),
        ).fetchall()


def remaining_steps(
    connection: sqlite3.Connection, plan: BundlePlan, version: DraftVersion
) -> tuple[tuple[Step, ...], set[Component]]:
    """The steps still to do, and the ones that must never be repeated.

    A component already confirmed is skipped. A component whose outcome is unknown
    blocks the whole resume: sending it again could duplicate a real message, and
    deciding which happened is a human's job with the channel in front of them.
    """
    components = ComponentRepository(connection)
    done = components.succeeded(version.id)
    unknown = components.uncertain(version.id)
    todo = tuple(step for step in plan.steps if step.component not in done)
    return todo, unknown


def execute(
    connection: sqlite3.Connection,
    client: TelegramClient,
    plan: BundlePlan,
    version: DraftVersion,
    *,
    publication_id: UUID,
    draft_id: UUID,
    channel: str,
    discussion_chat_id: int | None,
    media_root: Path,
) -> list[ComponentOutcome]:
    """Run the remaining steps, recording each as it completes.

    Raises:
        PublicationOutcomeUncertainError: a step's result was lost. Everything before it
            is recorded; nothing after it is attempted.
        TelegramError: a definite failure. Same rule — earlier steps stand.
    """
    components = ComponentRepository(connection)
    todo, unknown = remaining_steps(connection, plan, version)

    if unknown:
        raise PublicationOutcomeUncertainError(
            f"an earlier attempt left {', '.join(sorted(c.value for c in unknown))} in an "
            "unknown state, so this bundle may already be partly published. Check the "
            "channel and resolve it by hand before retrying."
        )

    outcomes: list[ComponentOutcome] = []
    main_message_id: int | None = None

    for step in todo:
        target = str(discussion_chat_id) if step.to_discussion else channel
        if step.to_discussion and discussion_chat_id is None:  # pragma: no cover
            continue

        try:
            outcome = run_step(client, step, target, media_root, main_message_id)
        except PublicationOutcomeUncertainError as exc:
            components.add(
                publication_id=publication_id,
                draft_id=draft_id,
                draft_version_id=version.id,
                outcome=ComponentOutcome(
                    component=step.component,
                    method=step.method,
                    status=PublicationStatus.UNCERTAIN,
                    failure_reason=str(exc),
                ),
            )
            logger.error(
                "bundle component outcome unknown",
                extra={"component": step.component.value, "draft_id": str(draft_id)},
            )
            raise
        except TelegramError as exc:
            components.add(
                publication_id=publication_id,
                draft_id=draft_id,
                draft_version_id=version.id,
                outcome=ComponentOutcome(
                    component=step.component,
                    method=step.method,
                    status=PublicationStatus.FAILED,
                    failure_reason=str(exc),
                ),
            )
            logger.error(
                "bundle component failed",
                extra={"component": step.component.value, "draft_id": str(draft_id)},
            )
            raise

        components.add(
            publication_id=publication_id,
            draft_id=draft_id,
            draft_version_id=version.id,
            outcome=outcome,
        )
        outcomes.append(outcome)
        if step.component is Component.MAIN:
            main_message_id = outcome.message_id

    for component, reason in plan.deferred:
        deferred = ComponentOutcome(
            component=component, method="-", status="DEFERRED", failure_reason=reason
        )
        components.add(
            publication_id=publication_id,
            draft_id=draft_id,
            draft_version_id=version.id,
            outcome=deferred,
        )
        outcomes.append(deferred)

    return outcomes


def run_step(
    client: TelegramClient,
    step: Step,
    target: str,
    media_root: Path,
    main_message_id: int | None,
) -> ComponentOutcome:
    """Make one call and turn the answer into an outcome."""
    if step.method == "sendMessage":
        payload: dict[str, object] = {
            "chat_id": target,
            "text": step.text,
            "link_preview_options": {"is_disabled": True},
        }
        if step.to_discussion and main_message_id is not None:
            # A channel comment is a reply to the forwarded copy of the post in the
            # linked group. Bot API 10.2 spells this reply_parameters.
            payload["reply_parameters"] = {"message_id": main_message_id}
        sent = client.send_message(payload)
    elif step.method == "sendPhoto":
        sent = client.send_photo(
            target, check_asset(step.assets[0], media_root), caption=step.text
        )
    elif step.method == "sendDocument":
        sent = client.send_document(target, check_asset(step.assets[0], media_root))
    elif step.method == "sendMediaGroup":
        messages = client.send_media_group(
            target, [check_asset(a, media_root) for a in step.assets]
        )
        sent = messages[0]
    else:  # pragma: no cover - the plan only emits the four above
        raise TelegramError(f"unsupported step method {step.method!r}")

    return ComponentOutcome(
        component=step.component,
        method=step.method,
        status=PublicationStatus.SUCCEEDED,
        message_id=sent.message_id,
        chat_id=sent.chat_id,
    )
