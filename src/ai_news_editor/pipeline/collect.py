"""Collection orchestrator.

The only layer that combines adapters with storage. Per source it loads fetch state,
calls the adapter, persists new raw items, and records the outcome.

One failing source never stops the others: each is wrapped so that even an unexpected
exception becomes a reported error rather than an aborted run. Nothing is swallowed —
every failure ends up in the report and in the logs.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from ai_news_editor.domain.clock import now_utc
from ai_news_editor.domain.enums import FetchOutcome
from ai_news_editor.domain.models import Source
from ai_news_editor.observability.logging import get_logger
from ai_news_editor.sources.base import FetchContext, FetchResult
from ai_news_editor.sources.config import SourceDefinition, SourcesConfig
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.sources.registry import build_adapter
from ai_news_editor.storage.repositories import (
    RawItemRepository,
    SourceFetchStateRepository,
    SourceRepository,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SourceReport:
    """What happened for one source."""

    source_id: str
    name: str
    outcome: FetchOutcome
    fetched: int = 0
    inserted: int = 0
    existing: int = 0
    http_status: int | None = None
    error: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome is not FetchOutcome.ERROR


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """The result of one collection run."""

    run_id: str
    sources: tuple[SourceReport, ...]
    dry_run: bool = False

    @property
    def succeeded(self) -> int:
        return sum(1 for report in self.sources if report.ok)

    @property
    def failed(self) -> int:
        return sum(1 for report in self.sources if not report.ok)

    @property
    def inserted(self) -> int:
        return sum(report.inserted for report in self.sources)

    @property
    def existing(self) -> int:
        return sum(report.existing for report in self.sources)

    @property
    def fetched(self) -> int:
        return sum(report.fetched for report in self.sources)

    @property
    def all_ok(self) -> bool:
        return self.failed == 0


def collect(
    connection: sqlite3.Connection,
    http: HttpClient,
    config: SourcesConfig,
    *,
    run_id: str,
    source_ids: list[str] | None = None,
    dry_run: bool = False,
) -> CollectionReport:
    """Collect from the selected sources.

    Args:
        source_ids: restrict to these ids; ``None`` means every enabled source.
        dry_run: fetch and parse, but write nothing at all — neither raw items nor
            fetch state. A dry run must not change what the next real run does.
    """
    definitions = _select(config, source_ids)
    sources_repo = SourceRepository(connection)
    state_repo = SourceFetchStateRepository(connection)
    raw_repo = RawItemRepository(connection)

    logger.info(
        "collection started",
        extra={"sources": len(definitions), "dry_run": dry_run},
    )

    reports: list[SourceReport] = []
    for definition in definitions:
        source = definition.to_source(config.defaults)
        if not dry_run:
            # Keep the persisted source row in step with configuration; raw items
            # reference it by foreign key.
            source = sources_repo.upsert(source)

        reports.append(
            _collect_one(
                definition=definition,
                source=source,
                config=config,
                http=http,
                state_repo=state_repo,
                raw_repo=raw_repo,
                run_id=run_id,
                dry_run=dry_run,
            )
        )

    report = CollectionReport(run_id=run_id, sources=tuple(reports), dry_run=dry_run)
    logger.info(
        "collection finished",
        extra={
            "succeeded": report.succeeded,
            "failed": report.failed,
            "inserted": report.inserted,
            "existing": report.existing,
            "dry_run": dry_run,
        },
    )
    return report


def _select(config: SourcesConfig, source_ids: list[str] | None) -> list[SourceDefinition]:
    """Resolve which sources to read.

    An explicitly named source is collected even if disabled — the operator asked for
    it by name, which is a clearer signal of intent than the config default.
    """
    if source_ids:
        return [config.get(source_id) for source_id in source_ids]
    return config.enabled()


def _collect_one(
    *,
    definition: SourceDefinition,
    source: Source,
    config: SourcesConfig,
    http: HttpClient,
    state_repo: SourceFetchStateRepository,
    raw_repo: RawItemRepository,
    run_id: str,
    dry_run: bool,
) -> SourceReport:
    started = time.monotonic()
    state = state_repo.get(source.id)
    context = FetchContext(
        run_id=run_id,
        etag=state.etag,
        last_modified=state.last_modified,
        max_items=definition.max_items(config.defaults),
    )

    try:
        adapter = build_adapter(source.kind, http)
        result = adapter.fetch(source, context)
    except Exception as exc:
        logger.exception("source failed unexpectedly", extra={"source_id": source.id})
        result = FetchResult.failed(source.id, f"{type(exc).__name__}: {exc}")

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if result.outcome is FetchOutcome.ERROR:
        if not dry_run:
            state_repo.record_failure(
                source.id, error=result.error or "unknown error", http_status=result.http_status
            )
        logger.warning(
            "source fetch failed",
            extra={
                "source_id": source.id,
                "error": result.error,
                "http_status": result.http_status,
            },
        )
        return SourceReport(
            source_id=source.id,
            name=source.name,
            outcome=FetchOutcome.ERROR,
            http_status=result.http_status,
            error=result.error,
            duration_ms=elapsed_ms,
        )

    inserted = 0
    if not dry_run:
        for item in result.items:
            if raw_repo.add_if_absent(item):
                inserted += 1
        state_repo.record_success(
            source.id,
            outcome=result.outcome,
            etag=result.etag,
            last_modified=result.last_modified,
            http_status=result.http_status,
        )
    else:
        # Nothing is written, so report what *would* be new by asking the database.
        # Identities are deduplicated within the batch too, because a feed can repeat a
        # guid; counting it twice would make the dry run over-predict the real result.
        unseen: set[str] = set()
        for item in result.items:
            if not item.external_id:
                continue
            if item.external_id in unseen:
                continue
            if not raw_repo.exists_external_id(source.id, item.external_id):
                unseen.add(item.external_id)
        inserted = len(unseen)

    fetched = len(result.items)
    logger.info(
        "source collected",
        extra={
            "source_id": source.id,
            "outcome": result.outcome.value,
            "fetched": fetched,
            "inserted": inserted,
            "existing": fetched - inserted,
            "http_status": result.http_status,
        },
    )
    for warning in result.warnings:
        logger.warning("parse warning", extra={"source_id": source.id, "detail": warning})

    return SourceReport(
        source_id=source.id,
        name=source.name,
        outcome=result.outcome,
        fetched=fetched,
        inserted=inserted,
        existing=fetched - inserted,
        http_status=result.http_status,
        warnings=result.warnings,
        duration_ms=elapsed_ms,
    )


def collection_timestamp() -> str:
    """Human-readable UTC stamp for report headers."""
    return now_utc().strftime("%Y-%m-%d %H:%M UTC")
