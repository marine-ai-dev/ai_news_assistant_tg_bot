"""Hacker News community-signal adapter.

The behavioural line these tests defend: HN tells us people are *talking* about
something. It never tells us anything is true, and none of its text becomes article
content.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ai_news_editor.domain.enums import FetchOutcome, SourceKind, TrustTier
from ai_news_editor.domain.models import Source
from ai_news_editor.sources.base import FetchContext, FetchResult
from ai_news_editor.sources.config import load_sources_config
from ai_news_editor.sources.hn_algolia import HnSignalAdapter, HnSignalOptions, signal_fields
from tests.conftest import make_http_client, static_transport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hn"
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"


def payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def hn_source(**overrides: object) -> Source:
    config = load_sources_config(REPO_CONFIG)
    source = config.get("hackernews").to_source(config.defaults)
    for key, value in overrides.items():
        setattr(source, key, value)
    return source


def fetch(body: bytes, *, source: Source | None = None, **response: object) -> FetchResult:
    adapter = HnSignalAdapter(
        make_http_client(static_transport(body, content_type="application/json", **response))  # type: ignore[arg-type]
    )
    return adapter.fetch(source or hn_source(), FetchContext(run_id="testrun", max_items=50))


class TestParsing:
    def test_stories_with_links_become_items(self) -> None:
        result = fetch(payload("stories.json"))
        assert result.outcome is FetchOutcome.OK
        assert len(result.items) == 2

    def test_ask_hn_posts_are_skipped(self) -> None:
        """A self post has no external story to be a signal about."""
        urls = {item.url_original for item in fetch(payload("stories.json")).items}
        assert not any("ask" in url.lower() for url in urls)

    def test_untitled_stories_are_skipped(self) -> None:
        result = fetch(payload("stories.json"))
        assert all(item.title_original for item in result.items)
        assert any("no title" in warning for warning in result.warnings)

    def test_object_id_is_the_identity(self) -> None:
        ids = {item.external_id for item in fetch(payload("stories.json")).items}
        assert "49243709" in ids

    def test_timestamps_are_utc(self) -> None:
        item = next(i for i in fetch(payload("stories.json")).items if i.external_id == "49243709")
        assert item.published_at == datetime(2026, 8, 10, 13, 54, 55, tzinfo=UTC)

    def test_author_is_captured(self) -> None:
        item = next(i for i in fetch(payload("stories.json")).items if i.external_id == "49243709")
        assert item.author == "mellosouls"

    def test_empty_results_are_not_an_error(self) -> None:
        result = fetch(payload("empty.json"))
        assert result.outcome is FetchOutcome.OK
        assert result.items == ()


class TestNoContentIsImported:
    def test_no_article_text_is_taken_from_hn(self) -> None:
        """HN supplies discussion, not journalism. None of it may become article body."""
        for item in fetch(payload("stories.json")).items:
            assert item.summary_raw is None
            assert item.content_raw is None

    def test_comment_counts_are_metadata_not_text(self) -> None:
        item = next(i for i in fetch(payload("stories.json")).items if i.external_id == "49243709")
        extra = signal_fields(item.payload_raw)
        assert extra["num_comments"] == 233
        assert extra["points"] == 412

    def test_discussion_url_points_at_hacker_news(self) -> None:
        item = next(i for i in fetch(payload("stories.json")).items if i.external_id == "49243709")
        assert signal_fields(item.payload_raw)["discussion_url"] == (
            "https://news.ycombinator.com/item?id=49243709"
        )

    def test_signal_fields_tolerates_junk(self) -> None:
        assert signal_fields("not json") == {}


class TestTrustSemantics:
    def test_the_configured_source_is_community_signal_and_signal_only(self) -> None:
        source = hn_source()
        assert source.trust_tier is TrustTier.COMMUNITY_SIGNAL
        assert source.signal_only is True
        assert source.kind is SourceKind.HN_SIGNAL

    def test_a_community_source_cannot_be_configured_as_authoritative(self) -> None:
        """The domain refuses to represent community chatter as a normal source."""
        with pytest.raises(ValueError, match="signal_only"):
            Source(
                id="hn",
                name="HN",
                kind=SourceKind.HN_SIGNAL,
                url="https://hn.algolia.com/api/v1/search_by_date",
                trust_tier=TrustTier.COMMUNITY_SIGNAL,
                signal_only=False,
            )

    def test_fetching_does_not_change_the_trust_tier(self) -> None:
        source = hn_source()
        fetch(payload("stories.json"), source=source)
        assert source.trust_tier is TrustTier.COMMUNITY_SIGNAL


class TestBoundedQuerying:
    def _capture(self, source: Source) -> list[str]:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(
                200, content=payload("empty.json"), headers={"content-type": "application/json"}
            )

        HnSignalAdapter(make_http_client(httpx.MockTransport(handler))).fetch(
            source, FetchContext(run_id="r")
        )
        return requested

    def test_one_request_per_configured_query(self) -> None:
        source = hn_source()
        source.config = {"queries": ["a", "b", "c"], "window_hours": 24, "hits_per_query": 10}
        assert len(self._capture(source)) == 3

    def test_requests_are_time_bounded(self) -> None:
        source = hn_source()
        source.config = {"queries": ["ai"], "window_hours": 24}
        params = parse_qs(urlsplit(self._capture(source)[0]).query)
        assert "created_at_i>" in params["numericFilters"][0]

    def test_requests_are_size_bounded(self) -> None:
        source = hn_source()
        source.config = {"queries": ["ai"], "hits_per_query": 25}
        params = parse_qs(urlsplit(self._capture(source)[0]).query)
        assert params["hitsPerPage"] == ["25"]

    def test_only_stories_are_requested(self) -> None:
        params = parse_qs(urlsplit(self._capture(hn_source())[0]).query)
        assert params["tags"] == ["story"]

    def test_low_score_stories_are_filtered_at_the_api(self) -> None:
        source = hn_source()
        source.config = {"queries": ["ai"], "min_points": 25}
        params = parse_qs(urlsplit(self._capture(source)[0]).query)
        assert "points>=25" in params["numericFilters"][0]

    def test_duplicate_stories_across_queries_are_recorded_once(self) -> None:
        source = hn_source()
        source.config = {"queries": ["one", "two"]}
        adapter = HnSignalAdapter(
            make_http_client(
                static_transport(payload("stories.json"), content_type="application/json")
            )
        )
        result = adapter.fetch(source, FetchContext(run_id="r"))
        assert len(result.items) == 2

    def test_option_limits_are_enforced(self) -> None:
        with pytest.raises(ValueError):
            HnSignalOptions.model_validate({"hits_per_query": 5000})
        with pytest.raises(ValueError):
            HnSignalOptions.model_validate({"window_hours": 100000})


class TestFailureModes:
    def test_api_error_is_typed(self) -> None:
        result = fetch(b"", status_code=500)
        assert result.outcome is FetchOutcome.ERROR
        assert result.http_status == 500

    def test_invalid_json_is_reported(self) -> None:
        result = fetch(b"{not json")
        assert result.outcome is FetchOutcome.ERROR
        assert "invalid JSON" in (result.error or "")

    def test_unexpected_shape_is_reported(self) -> None:
        result = fetch(json.dumps({"unexpected": True}).encode())
        assert result.outcome is FetchOutcome.ERROR
        assert "unexpected HN API response shape" in (result.error or "")

    def test_invalid_options_are_reported(self) -> None:
        source = hn_source()
        source.config = {"not_a_real_option": 1}
        result = fetch(payload("stories.json"), source=source)
        assert result.outcome is FetchOutcome.ERROR
        assert "invalid hn_signal options" in (result.error or "")

    def test_timeout_is_typed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        adapter = HnSignalAdapter(make_http_client(httpx.MockTransport(handler)))
        result = adapter.fetch(hn_source(), FetchContext(run_id="r"))
        assert result.outcome is FetchOutcome.ERROR
