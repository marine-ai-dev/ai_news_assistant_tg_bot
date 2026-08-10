"""HTML changelog adapter, against recorded page fixtures.

Fixtures are snapshots, deliberately not today's live HTML: a test that depends on a
vendor's current markup fails for reasons that have nothing to do with our code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_news_editor.domain.enums import FetchOutcome, SourceKind, TrustTier
from ai_news_editor.domain.models import Source
from ai_news_editor.sources.base import FetchContext, FetchResult
from ai_news_editor.sources.config import load_sources_config
from ai_news_editor.sources.html_changelog import HtmlChangelogAdapter, HtmlChangelogOptions
from tests.conftest import make_http_client, static_transport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "html"
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"


def page(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def source_from_config(source_id: str) -> Source:
    """Build the source exactly as the shipped configuration defines it.

    Testing the real selectors rather than test-only ones: a selector typo in
    config/sources.yaml should fail here, not in production.
    """
    config = load_sources_config(REPO_CONFIG)
    return config.get(source_id).to_source(config.defaults)


def fetch(source: Source, body: bytes, **response: object) -> FetchResult:
    adapter = HtmlChangelogAdapter(
        make_http_client(static_transport(body, content_type="text/html", **response))  # type: ignore[arg-type]
    )
    return adapter.fetch(source, FetchContext(run_id="testrun", max_items=50))


class TestAnthropicNewsroom:
    @pytest.fixture
    def result(self) -> FetchResult:
        return fetch(source_from_config("anthropic_news"), page("anthropic_news.html"))

    def test_extracts_every_entry(self, result: FetchResult) -> None:
        assert result.outcome is FetchOutcome.OK
        assert len(result.items) == 5

    def test_handles_both_page_layouts(self, result: FetchResult) -> None:
        """The newsroom mixes a heading-based grid with a span-based list.

        Live collection surfaced this: ten real posts were being skipped because only
        the grid layout was matched.
        """
        titles = {item.title_original for item in result.items}
        assert "Introducing Claude Opus 5" in titles
        assert "A list-layout post whose title is a span" in titles

    def test_extracts_titles(self, result: FetchResult) -> None:
        titles = {item.title_original for item in result.items}
        assert "Introducing Claude Opus 5" in titles
        assert "Inviting hard questions" in titles

    def test_resolves_relative_links_to_absolute_urls(self, result: FetchResult) -> None:
        urls = {item.url_original for item in result.items}
        assert "https://www.anthropic.com/news/claude-opus-5" in urls
        assert all(url.startswith("https://") for url in urls)

    def test_extracts_dates(self, result: FetchResult) -> None:
        item = next(i for i in result.items if i.url_original.endswith("/claude-opus-5"))
        assert item.published_at == datetime(2026, 7, 24, tzinfo=UTC)

    def test_extracts_summaries(self, result: FetchResult) -> None:
        item = next(i for i in result.items if i.url_original.endswith("/claude-opus-5"))
        assert item.summary_raw is not None
        assert "step change" in item.summary_raw

    def test_decodes_entities_in_summaries(self, result: FetchResult) -> None:
        item = next(i for i in result.items if i.url_original.endswith("/hard-questions"))
        assert "’" in (item.summary_raw or "") or "'" in (item.summary_raw or "")
        assert "&#8217;" not in (item.summary_raw or "")

    def test_missing_date_stays_absent(self, result: FetchResult) -> None:
        """A missing date is recorded as missing, never guessed."""
        item = next(i for i in result.items if i.url_original.endswith("/no-date-entry"))
        assert item.published_at is None

    def test_non_ascii_content_survives(self, result: FetchResult) -> None:
        item = next(i for i in result.items if i.url_original.endswith("/emoji-and-unicode"))
        assert item.title_original == "Новина про штучний інтелект 🤖"

    def test_scripts_and_navigation_are_not_captured(self, result: FetchResult) -> None:
        blob = " ".join(f"{i.title_original} {i.summary_raw or ''}" for i in result.items)
        assert "never appear as text" not in blob
        assert "__DATA__" not in blob

    def test_provenance_is_recorded(self, result: FetchResult) -> None:
        for item in result.items:
            assert item.source_id == "anthropic_news"
            assert item.external_id == item.url_original
            assert item.payload_raw
            assert item.content_type == "text/html"


class TestNotionReleases:
    @pytest.fixture
    def result(self) -> FetchResult:
        return fetch(source_from_config("notion_releases"), page("notion_releases.html"))

    def test_extracts_entries(self, result: FetchResult) -> None:
        assert result.outcome is FetchOutcome.OK
        assert len(result.items) == 3

    def test_extracts_titles_from_headings(self, result: FetchResult) -> None:
        assert "High contrast mode" in {item.title_original for item in result.items}

    def test_extracts_the_date_from_the_url(self, result: FetchResult) -> None:
        item = next(i for i in result.items if i.url_original.endswith("/2026-08-07"))
        assert item.published_at == datetime(2026, 8, 7, tzinfo=UTC)

    def test_links_are_absolute(self, result: FetchResult) -> None:
        assert all(
            i.url_original.startswith("https://www.notion.com/releases/") for i in result.items
        )


class TestSelectorBreakage:
    """A redesign must fail loudly, not look like a quiet week."""

    def test_zero_matches_is_reported_as_an_error(self) -> None:
        result = fetch(source_from_config("anthropic_news"), page("redesigned_no_items.html"))
        assert result.outcome is FetchOutcome.ERROR
        assert result.items == ()

    def test_the_error_names_the_selector_that_stopped_matching(self) -> None:
        result = fetch(source_from_config("anthropic_news"), page("redesigned_no_items.html"))
        assert "a[href^=" in (result.error or "")
        assert "structure has probably changed" in (result.error or "")

    def test_too_few_matches_also_fails(self) -> None:
        source = source_from_config("notion_releases")
        source.config = {**source.config, "min_expected_items": 10}
        result = fetch(source, page("notion_releases.html"))
        assert result.outcome is FetchOutcome.ERROR
        assert "expected at least 10" in (result.error or "")

    def test_a_healthy_page_does_not_trip_the_check(self) -> None:
        assert fetch(source_from_config("notion_releases"), page("notion_releases.html")).ok


class TestFailureModes:
    def _source(self) -> Source:
        return source_from_config("anthropic_news")

    def test_http_error_is_typed(self) -> None:
        result = fetch(self._source(), b"", status_code=503)
        assert result.outcome is FetchOutcome.ERROR
        assert result.http_status == 503

    def test_304_is_success(self) -> None:
        result = fetch(self._source(), b"", status_code=304)
        assert result.outcome is FetchOutcome.NOT_MODIFIED
        assert result.ok

    def test_invalid_options_are_reported(self) -> None:
        source = Source(
            id="broken",
            name="Broken",
            kind=SourceKind.HTML_CHANGELOG,
            url="https://broken.invalid/news",
            trust_tier=TrustTier.OFFICIAL,
            config={"nonsense_option": True},
        )
        result = fetch(source, page("anthropic_news.html"))
        assert result.outcome is FetchOutcome.ERROR
        assert "invalid html_changelog options" in (result.error or "")

    def test_item_cap_is_honoured(self) -> None:
        adapter = HtmlChangelogAdapter(
            make_http_client(
                static_transport(page("anthropic_news.html"), content_type="text/html")
            )
        )
        result = adapter.fetch(self._source(), FetchContext(run_id="r", max_items=2))
        assert len(result.items) == 2


class TestOptionsValidation:
    def test_item_selector_is_required(self) -> None:
        with pytest.raises(ValueError, match="item_selector"):
            HtmlChangelogOptions.model_validate({})

    def test_unknown_option_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            HtmlChangelogOptions.model_validate({"item_selector": "a", "typo": 1})

    def test_defaults_are_sane(self) -> None:
        options = HtmlChangelogOptions.model_validate({"item_selector": "a"})
        assert options.link_selector == ":self"
        assert options.min_expected_items >= 1


class TestNoPageFollowing:
    def test_only_the_listing_page_is_requested(self) -> None:
        """The adapter must not become a crawler by fetching each linked article."""
        requested: list[str] = []

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(
                200, content=page("anthropic_news.html"), headers={"content-type": "text/html"}
            )

        adapter = HtmlChangelogAdapter(make_http_client(httpx.MockTransport(handler)))
        adapter.fetch(source_from_config("anthropic_news"), FetchContext(run_id="r"))
        assert requested == ["https://www.anthropic.com/news"]
