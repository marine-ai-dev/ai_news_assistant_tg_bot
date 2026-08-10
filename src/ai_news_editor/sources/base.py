"""The source adapter contract.

An adapter does exactly one thing: read an external source and return what it found as
domain objects. It does not touch the database, does not know repositories exist, does
not rank or normalize, and never calls an LLM. The pipeline layer coordinates adapters
with storage.

Adapters do not raise for expected failures. A timeout, a 404 or malformed XML all come
back as a :class:`FetchResult` with ``outcome=ERROR``, so one bad source can never abort
a collection run by propagating an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable

from ai_news_editor.domain.enums import FetchOutcome, SourceKind
from ai_news_editor.domain.models import RawItem, Source

#: Cap on items taken from a single fetch. Some feeds carry their whole archive — the
#: OpenAI feed is over a thousand entries — and ingesting all of it on every run is
#: neither useful nor polite. Feeds are newest-first, so the cap keeps the recent items.
DEFAULT_MAX_ITEMS = 50


@dataclass(frozen=True, slots=True)
class FetchContext:
    """Everything an adapter needs beyond the source definition itself."""

    run_id: str
    etag: str | None = None
    last_modified: str | None = None
    max_items: int = DEFAULT_MAX_ITEMS


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The typed outcome of one fetch attempt."""

    source_id: str
    outcome: FetchOutcome
    items: tuple[RawItem, ...] = ()
    etag: str | None = None
    last_modified: str | None = None
    http_status: int | None = None
    error: str | None = None
    #: Non-fatal parsing problems, e.g. a single entry skipped for having no link.
    #: Surfaced so degraded parsing is visible rather than silent.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Whether the source was read successfully. 304 counts as success."""
        return self.outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED)

    @classmethod
    def not_modified(
        cls, source_id: str, *, etag: str | None, last_modified: str | None
    ) -> FetchResult:
        return cls(
            source_id=source_id,
            outcome=FetchOutcome.NOT_MODIFIED,
            etag=etag,
            last_modified=last_modified,
            http_status=304,
        )

    @classmethod
    def failed(cls, source_id: str, error: str, *, http_status: int | None = None) -> FetchResult:
        return cls(
            source_id=source_id,
            outcome=FetchOutcome.ERROR,
            error=error,
            http_status=http_status,
        )


@runtime_checkable
class SourceAdapter(Protocol):
    """Reads one kind of external source."""

    kind: ClassVar[SourceKind]

    def fetch(self, source: Source, context: FetchContext) -> FetchResult:
        """Read ``source`` and return what was found. Must not raise for I/O failures."""
        ...
