"""Contract every source adapter must satisfy.

Written against the protocol, not against the RSS implementation, so a Phase-3 adapter
inherits the whole suite by being added to ``ADAPTER_CASES``. The point is observable
behaviour — what an adapter returns and what it refuses to do — not how it works
inside.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from ai_news_editor.domain.enums import FetchOutcome, SourceKind
from ai_news_editor.domain.models import RawItem, Source
from ai_news_editor.sources.base import FetchContext, FetchResult, SourceAdapter
from ai_news_editor.sources.config import load_sources_config
from ai_news_editor.sources.http import HttpClient
from ai_news_editor.sources.registry import build_adapter, supported_kinds
from tests.conftest import (
    feed_bytes,
    hn_bytes,
    html_bytes,
    make_http_client,
    rss_source,
    static_transport,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"


def html_source() -> Source:
    """A source using the real shipped Anthropic selectors."""
    config = load_sources_config(REPO_CONFIG)
    return config.get("anthropic_news").to_source(config.defaults)


def hn_source() -> Source:
    config = load_sources_config(REPO_CONFIG)
    return config.get("hackernews").to_source(config.defaults)


@dataclass(frozen=True)
class AdapterCase:
    """One adapter plus the fixtures needed to exercise it."""

    kind: SourceKind
    make_source: Callable[[], Source]
    good_body: bytes
    good_content_type: str
    junk_body: bytes


ADAPTER_CASES = [
    AdapterCase(
        kind=SourceKind.RSS,
        make_source=rss_source,
        good_body=feed_bytes("rss_full.xml"),
        good_content_type="application/rss+xml",
        junk_body=feed_bytes("truncated.xml"),
    ),
    AdapterCase(
        kind=SourceKind.HTML_CHANGELOG,
        make_source=html_source,
        good_body=html_bytes("anthropic_news.html"),
        good_content_type="text/html",
        junk_body=html_bytes("redesigned_no_items.html"),
    ),
    AdapterCase(
        kind=SourceKind.HN_SIGNAL,
        make_source=hn_source,
        good_body=hn_bytes("stories.json"),
        good_content_type="application/json",
        junk_body=b"{not json at all",
    ),
]

CASE_IDS = [case.kind.value for case in ADAPTER_CASES]


def _adapter(case: AdapterCase, body: bytes, **response: object) -> SourceAdapter:
    transport = static_transport(body, content_type=case.good_content_type, **response)  # type: ignore[arg-type]
    return build_adapter(case.kind, make_http_client(transport))


@pytest.mark.parametrize("case", ADAPTER_CASES, ids=CASE_IDS)
class TestAdapterContract:
    def test_every_registered_kind_has_a_case(self, case: AdapterCase) -> None:
        """Guards against adding an adapter and forgetting to contract-test it."""
        assert {c.kind for c in ADAPTER_CASES} == supported_kinds()

    def test_declares_the_kind_it_handles(self, case: AdapterCase) -> None:
        adapter = _adapter(case, case.good_body)
        assert adapter.kind is case.kind

    def test_satisfies_the_protocol(self, case: AdapterCase) -> None:
        assert isinstance(_adapter(case, case.good_body), SourceAdapter)

    def test_returns_a_fetch_result(self, case: AdapterCase, fetch_context: FetchContext) -> None:
        result = _adapter(case, case.good_body).fetch(case.make_source(), fetch_context)
        assert isinstance(result, FetchResult)
        assert result.outcome is FetchOutcome.OK
        assert result.source_id == case.make_source().id

    def test_returns_raw_items_with_provenance(
        self, case: AdapterCase, fetch_context: FetchContext
    ) -> None:
        source = case.make_source()
        result = _adapter(case, case.good_body).fetch(source, fetch_context)

        assert result.items
        for item in result.items:
            assert isinstance(item, RawItem)
            assert item.source_id == source.id
            assert item.url_original
            assert item.payload_raw
            assert item.fetched_at.tzinfo is not None
            assert item.fetch_run_id == fetch_context.run_id

    def test_produces_stable_identity_across_fetches(
        self, case: AdapterCase, fetch_context: FetchContext
    ) -> None:
        """Two reads of an unchanged source must yield the same external ids."""
        first = _adapter(case, case.good_body).fetch(case.make_source(), fetch_context)
        second = _adapter(case, case.good_body).fetch(case.make_source(), fetch_context)

        assert [i.external_id for i in first.items] == [i.external_id for i in second.items]
        assert all(i.external_id for i in first.items)

    def test_honours_the_item_cap(self, case: AdapterCase) -> None:
        capped = _adapter(case, case.good_body).fetch(
            case.make_source(), FetchContext(run_id="r", max_items=2)
        )
        assert len(capped.items) <= 2

    def test_malformed_input_is_a_typed_error_not_an_exception(
        self, case: AdapterCase, fetch_context: FetchContext
    ) -> None:
        result = _adapter(case, case.junk_body).fetch(case.make_source(), fetch_context)
        assert result.outcome is FetchOutcome.ERROR
        assert result.error
        assert result.items == ()

    def test_http_failure_is_a_typed_error_not_an_exception(
        self, case: AdapterCase, fetch_context: FetchContext
    ) -> None:
        result = _adapter(case, b"", status_code=404).fetch(case.make_source(), fetch_context)
        assert result.outcome is FetchOutcome.ERROR
        assert result.http_status == 404

    def test_timeout_is_a_typed_error_not_an_exception(
        self, case: AdapterCase, fetch_context: FetchContext
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        adapter = build_adapter(case.kind, make_http_client(httpx.MockTransport(handler)))
        result = adapter.fetch(case.make_source(), fetch_context)
        assert result.outcome is FetchOutcome.ERROR
        assert "timeout" in (result.error or "").lower()

    def test_304_is_success_with_no_items(
        self, case: AdapterCase, fetch_context: FetchContext
    ) -> None:
        adapter = _adapter(case, b"", status_code=304)
        result = adapter.fetch(case.make_source(), fetch_context)
        assert result.outcome is FetchOutcome.NOT_MODIFIED
        assert result.ok
        assert result.items == ()

    def test_adapter_never_writes_to_the_database(
        self, case: AdapterCase, fetch_context: FetchContext, connection: sqlite3.Connection
    ) -> None:
        """Adapters are pure I/O. Persistence is the pipeline's job, not theirs."""
        before = _row_counts(connection)
        _adapter(case, case.good_body).fetch(case.make_source(), fetch_context)
        assert _row_counts(connection) == before

    def test_adapter_takes_no_database_dependency(self, case: AdapterCase) -> None:
        adapter = _adapter(case, case.good_body)
        assert not any(isinstance(value, sqlite3.Connection) for value in vars(adapter).values())


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = ("sources", "raw_items", "articles", "drafts", "source_fetch_state")
    counts = {}
    for table in tables:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM " + table  # noqa: S608 - fixed literal table names
        ).fetchone()
        counts[table] = row["n"]
    return counts


class TestRegistry:
    def test_every_declared_kind_now_has_an_adapter(self) -> None:
        assert supported_kinds() == set(SourceKind)

    def test_unimplemented_kind_fails_loudly(self) -> None:
        """A source configured for a kind with no adapter must not be silently skipped."""
        from ai_news_editor.domain.errors import ConfigurationError
        from ai_news_editor.sources import registry

        client = make_http_client(static_transport(b""))
        with pytest.raises(ConfigurationError, match="no adapter implemented"):
            registry.build_adapter("NOT_A_REAL_KIND", client)  # type: ignore[arg-type]

    def test_builds_a_working_rss_adapter(self) -> None:
        adapter = build_adapter(SourceKind.RSS, make_http_client(static_transport(b"")))
        assert isinstance(adapter, SourceAdapter)


class TestFetchResultHelpers:
    def test_not_modified_is_ok(self) -> None:
        result = FetchResult.not_modified("s", etag='W/"x"', last_modified=None)
        assert result.ok
        assert result.http_status == 304

    def test_failed_is_not_ok(self) -> None:
        assert FetchResult.failed("s", "boom").ok is False

    def test_client_is_reusable_across_adapters(self) -> None:
        """One HTTP client is shared by the whole run; adapters must not own it."""
        client = make_http_client(static_transport(feed_bytes("rss_full.xml")))
        first = build_adapter(SourceKind.RSS, client)
        second = build_adapter(SourceKind.RSS, client)
        assert isinstance(first, SourceAdapter)
        assert isinstance(second, SourceAdapter)
        assert isinstance(client, HttpClient)
