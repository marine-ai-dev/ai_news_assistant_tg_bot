"""Shared fixtures.

Two guarantees this file provides for every test in the suite:

* **No network.** An autouse fixture makes any outbound socket connection raise. This
  phase has no integrations, and the guard is here so that the first accidental one
  fails a test instead of quietly reaching the internet.
* **No developer-machine state.** Databases live in ``tmp_path``, and environment
  variables that could steer configuration are cleared.
"""

from __future__ import annotations

import logging
import socket
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from ai_news_editor.domain.enums import AudienceTier, Category, SourceKind, TrustTier
from ai_news_editor.domain.models import Article, RawItem, Source
from ai_news_editor.settings import Settings
from ai_news_editor.sources.base import FetchContext
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.storage import db
from ai_news_editor.storage.repositories import (
    ArticleRepository,
    DraftRepository,
    RawItemRepository,
    ReviewDecisionRepository,
    SourceFetchStateRepository,
    SourceRepository,
)


class NetworkAccessDeniedError(RuntimeError):
    """Raised when a test attempts to open a network connection."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that tries to reach the network."""

    def deny(*args: object, **kwargs: object) -> None:
        raise NetworkAccessDeniedError(
            "tests must not access the network; this phase has no external integrations"
        )

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Detach handlers after each test.

    ``CliRunner`` swaps in a temporary stderr and closes it when the test ends; a
    handler left bound to it would break logging in every later test.
    """
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the developer's own environment from leaking into test configuration."""
    for name in list(dict(__import__("os").environ)):
        if name.startswith("AI_NEWS_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a throwaway data directory."""
    return Settings(data_dir=tmp_path, _env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated, empty database."""
    conn = db.connect(tmp_path / "test.sqlite3")
    db.migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def sources(connection: sqlite3.Connection) -> SourceRepository:
    return SourceRepository(connection)


@pytest.fixture
def raw_items(connection: sqlite3.Connection) -> RawItemRepository:
    return RawItemRepository(connection)


@pytest.fixture
def articles(connection: sqlite3.Connection) -> ArticleRepository:
    return ArticleRepository(connection)


@pytest.fixture
def drafts(connection: sqlite3.Connection) -> DraftRepository:
    return DraftRepository(connection)


@pytest.fixture
def decisions(connection: sqlite3.Connection) -> ReviewDecisionRepository:
    return ReviewDecisionRepository(connection)


# --- synthetic entities ----------------------------------------------------
# Obviously fabricated placeholders on the reserved .invalid TLD. These are structural
# fixtures for persistence tests, not sample news: no source is contacted and nothing
# here pretends to be collected content.


def make_source(source_id: str = "test_source", **overrides: object) -> Source:
    data: dict[str, object] = {
        "id": source_id,
        "name": "Test Source",
        "kind": SourceKind.RSS,
        "url": "https://example.invalid/feed.xml",
        "trust_tier": TrustTier.OFFICIAL,
        "publisher": "Example",
    }
    data.update(overrides)
    return Source.model_validate(data)


def make_raw_item(source_id: str = "test_source", **overrides: object) -> RawItem:
    data: dict[str, object] = {
        "source_id": source_id,
        "external_id": f"item-{uuid4().hex[:8]}",
        "title_original": "Placeholder item",
        "url_original": "https://example.invalid/item",
        "payload_raw": "{}",
        "content_type": "application/json",
    }
    data.update(overrides)
    return RawItem.model_validate(data)


def make_article(raw_item_id: UUID, source_id: str = "test_source", **overrides: object) -> Article:
    data: dict[str, object] = {
        "raw_item_id": raw_item_id,
        "source_id": source_id,
        "title": "Placeholder article",
        "canonical_url": "https://example.invalid/item",
    }
    data.update(overrides)
    return Article.model_validate(data)


@pytest.fixture
def seeded_article(
    sources: SourceRepository,
    raw_items: RawItemRepository,
    articles: ArticleRepository,
) -> Article:
    """A persisted source → raw item → article chain, ready for draft tests."""
    source = sources.upsert(make_source())
    item = raw_items.add(make_raw_item(source.id))
    return articles.add(make_article(item.id, source.id))


# --- HTTP test doubles -----------------------------------------------------

FIXTURE_FEEDS = Path(__file__).parent / "fixtures" / "feeds"


def feed_bytes(name: str) -> bytes:
    """Read a recorded feed fixture."""
    return (FIXTURE_FEEDS / name).read_bytes()


def mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    """Wrap a request handler as an httpx transport."""
    return httpx.MockTransport(handler)


def static_transport(
    body: bytes,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    content_type: str = "application/rss+xml",
) -> httpx.MockTransport:
    """A transport that always answers with the same response."""
    response_headers = {"content-type": content_type, **(headers or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body, headers=response_headers)

    return httpx.MockTransport(handler)


def make_http_client(transport: httpx.MockTransport, **overrides: object) -> HttpClient:
    """An HttpClient wired to a mock transport, with retry sleeps disabled."""
    kwargs: dict[str, object] = {"retry_backoff_seconds": 0.0, "transport": transport}
    kwargs.update(overrides)
    return HttpClient(**kwargs)  # type: ignore[arg-type]


def rss_source(source_id: str = "test_source", **overrides: object) -> Source:
    """A source pointing at a fake feed host."""
    data: dict[str, object] = {"kind": SourceKind.RSS, "url": "https://feed.invalid/rss.xml"}
    data.update(overrides)
    return make_source(source_id, **data)


@pytest.fixture
def fetch_context() -> FetchContext:
    return FetchContext(run_id="testrun", max_items=50)


@pytest.fixture
def fetch_states(connection: sqlite3.Connection) -> SourceFetchStateRepository:
    return SourceFetchStateRepository(connection)


DRAFT_CONTENT: dict[str, object] = {
    "title": "Placeholder draft",
    "body": "Placeholder body text.",
    "category": Category.PRODUCT_UPDATE,
    "audience": AudienceTier.BEGINNER,
    "source_attribution": "Example — https://example.invalid/item",
    "created_by": "test",
}
