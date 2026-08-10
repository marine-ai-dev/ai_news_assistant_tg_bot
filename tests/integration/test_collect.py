"""The collection pipeline end to end, against mock transports and a temp database."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from ai_news_editor.domain.enums import FetchOutcome
from ai_news_editor.pipeline.collect import collect
from ai_news_editor.sources.config import SourcesConfig
from ai_news_editor.storage.repositories import (
    RawItemRepository,
    SourceFetchStateRepository,
    SourceRepository,
)
from tests.conftest import feed_bytes, make_http_client

TWO_SOURCES = {
    "version": 1,
    "defaults": {"max_items_per_fetch": 50},
    "sources": [
        {
            "id": "alpha",
            "name": "Alpha Feed",
            "adapter": "rss",
            "url": "https://alpha.invalid/rss.xml",
            "trust_tier": "OFFICIAL",
            "editorial_role": "Primary test source.",
        },
        {
            "id": "beta",
            "name": "Beta Feed",
            "adapter": "rss",
            "url": "https://beta.invalid/atom.xml",
            "trust_tier": "REPUTABLE_SECONDARY",
            "editorial_role": "Secondary test source.",
        },
    ],
}


def config(*, only_alpha: bool = False) -> SourcesConfig:
    data = {**TWO_SOURCES}
    if only_alpha:
        data = {**TWO_SOURCES, "sources": TWO_SOURCES["sources"][:1]}
    return SourcesConfig.model_validate(data)


def routing_transport(responses: dict[str, httpx.Response | Exception]) -> httpx.MockTransport:
    """Answer per host, so several sources can behave differently in one run."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = responses[request.url.host]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(
            answer.status_code,
            content=answer.content,
            headers=dict(answer.headers),
        )

    return httpx.MockTransport(handler)


def rss_response(fixture: str = "rss_full.xml", **headers: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=feed_bytes(fixture),
        headers={"content-type": "application/rss+xml", **headers},
    )


def run(
    connection: sqlite3.Connection,
    responses: dict[str, httpx.Response | Exception],
    *,
    cfg: SourcesConfig | None = None,
    **kwargs: object,
):  # type: ignore[no-untyped-def]
    http = make_http_client(routing_transport(responses))
    try:
        return collect(connection, http, cfg or config(), run_id="testrun", **kwargs)  # type: ignore[arg-type]
    finally:
        http.close()


class TestSuccessfulCollection:
    def test_persists_items_from_every_source(self, connection: sqlite3.Connection) -> None:
        report = run(
            connection,
            {"alpha.invalid": rss_response(), "beta.invalid": rss_response("atom_full.xml")},
        )
        assert report.all_ok
        assert report.succeeded == 2
        # rss_full has 3 entries but repeats a guid, so it contributes 2 distinct items.
        assert RawItemRepository(connection).count() == 4

    def test_reports_per_source_counts(self, connection: sqlite3.Connection) -> None:
        report = run(
            connection,
            {"alpha.invalid": rss_response(), "beta.invalid": rss_response("atom_full.xml")},
        )
        by_id = {r.source_id: r for r in report.sources}
        assert by_id["alpha"].fetched == 3
        assert by_id["alpha"].inserted == 2
        assert by_id["alpha"].existing == 1
        assert by_id["beta"].fetched == 2
        assert by_id["beta"].inserted == 2

    def test_sources_are_synced_from_configuration(self, connection: sqlite3.Connection) -> None:
        run(connection, {"alpha.invalid": rss_response(), "beta.invalid": rss_response()})
        stored = SourceRepository(connection).get("alpha")
        assert stored.name == "Alpha Feed"
        assert stored.editorial_role == "Primary test source."

    def test_items_keep_their_source_link(self, connection: sqlite3.Connection) -> None:
        run(connection, {"alpha.invalid": rss_response(), "beta.invalid": rss_response()})
        items = RawItemRepository(connection).list_by_source("alpha")
        assert items
        assert all(item.source_id == "alpha" for item in items)

    def test_run_id_is_recorded_on_every_item(self, connection: sqlite3.Connection) -> None:
        run(connection, {"alpha.invalid": rss_response(), "beta.invalid": rss_response()})
        items = RawItemRepository(connection).list_by_source("alpha")
        assert all(item.fetch_run_id == "testrun" for item in items)


class TestIdempotency:
    def test_a_second_run_inserts_nothing_new(self, connection: sqlite3.Connection) -> None:
        """Even when the server returns the full body again, identity prevents duplicates."""
        responses = {"alpha.invalid": rss_response(), "beta.invalid": rss_response()}
        first = run(connection, responses)
        second = run(connection, responses)

        assert first.inserted == 4
        assert second.inserted == 0
        assert second.existing == 6
        assert RawItemRepository(connection).count() == 4

    def test_repeated_runs_stay_stable(self, connection: sqlite3.Connection) -> None:
        responses = {"alpha.invalid": rss_response(), "beta.invalid": rss_response()}
        for _ in range(4):
            run(connection, responses)
        assert RawItemRepository(connection).count() == 4

    def test_a_repeated_guid_within_one_feed_is_stored_once(
        self, connection: sqlite3.Connection
    ) -> None:
        """rss_full.xml repeats guid 0001; the second occurrence must not duplicate it."""
        report = run(connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True))
        assert report.sources[0].fetched == 3
        assert report.sources[0].inserted == 2
        assert report.sources[0].existing == 1

    def test_new_entries_are_added_on_a_later_run(self, connection: sqlite3.Connection) -> None:
        run(
            connection,
            {"alpha.invalid": rss_response("rss_minimal.xml")},
            cfg=config(only_alpha=True),
        )
        assert RawItemRepository(connection).count() == 2

        second = run(
            connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True)
        )
        assert second.inserted == 2
        assert RawItemRepository(connection).count() == 4


class TestConditionalFetching:
    def test_validators_are_stored_after_a_successful_fetch(
        self, connection: sqlite3.Connection
    ) -> None:
        run(
            connection,
            {
                "alpha.invalid": rss_response(
                    etag='W/"v1"',
                    **{"last-modified": "Mon, 03 Aug 2026 10:30:00 GMT"},
                )
            },
            cfg=config(only_alpha=True),
        )
        state = SourceFetchStateRepository(connection).get("alpha")
        assert state.etag == 'W/"v1"'
        assert state.last_modified == "Mon, 03 Aug 2026 10:30:00 GMT"
        assert state.last_outcome is FetchOutcome.OK
        assert state.last_success_at is not None

    def test_stored_validators_are_sent_on_the_next_run(
        self, connection: sqlite3.Connection
    ) -> None:
        run(
            connection,
            {"alpha.invalid": rss_response(etag='W/"v1"')},
            cfg=config(only_alpha=True),
        )

        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(304)

        http = make_http_client(httpx.MockTransport(handler))
        try:
            collect(connection, http, config(only_alpha=True), run_id="second")
        finally:
            http.close()
        assert seen["if-none-match"] == 'W/"v1"'

    def test_304_is_success_with_no_new_items(self, connection: sqlite3.Connection) -> None:
        run(connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True))
        before = RawItemRepository(connection).count()

        report = run(
            connection,
            {"alpha.invalid": httpx.Response(304)},
            cfg=config(only_alpha=True),
        )
        assert report.all_ok
        assert report.sources[0].outcome is FetchOutcome.NOT_MODIFIED
        assert report.sources[0].inserted == 0
        assert RawItemRepository(connection).count() == before

    def test_304_does_not_count_as_a_failure(self, connection: sqlite3.Connection) -> None:
        report = run(
            connection, {"alpha.invalid": httpx.Response(304)}, cfg=config(only_alpha=True)
        )
        assert report.failed == 0
        assert report.succeeded == 1

    def test_304_preserves_the_validator_that_produced_it(
        self, connection: sqlite3.Connection
    ) -> None:
        run(
            connection,
            {"alpha.invalid": rss_response(etag='W/"keep"')},
            cfg=config(only_alpha=True),
        )
        run(connection, {"alpha.invalid": httpx.Response(304)}, cfg=config(only_alpha=True))
        assert SourceFetchStateRepository(connection).get("alpha").etag == 'W/"keep"'

    def test_a_feed_without_validators_still_collects(
        self, connection: sqlite3.Connection
    ) -> None:
        """Several real feeds send no ETag; correctness must not depend on conditional GET."""
        report = run(connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True))
        state = SourceFetchStateRepository(connection).get("alpha")
        assert report.all_ok
        assert state.etag is None
        assert state.last_success_at is not None

        repeat = run(connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True))
        assert repeat.inserted == 0, "idempotent inserts cover what conditional GET cannot"


class TestFailureIsolation:
    def test_one_failing_source_does_not_stop_the_others(
        self, connection: sqlite3.Connection
    ) -> None:
        report = run(
            connection,
            {
                "alpha.invalid": httpx.Response(500),
                "beta.invalid": rss_response("atom_full.xml"),
            },
        )
        by_id = {r.source_id: r for r in report.sources}
        assert by_id["alpha"].outcome is FetchOutcome.ERROR
        assert by_id["beta"].outcome is FetchOutcome.OK
        assert by_id["beta"].inserted == 2
        assert report.succeeded == 1
        assert report.failed == 1

    def test_a_timeout_is_reported_not_raised(self, connection: sqlite3.Connection) -> None:
        report = run(
            connection,
            {
                "alpha.invalid": httpx.ReadTimeout("slow"),
                "beta.invalid": rss_response("atom_full.xml"),
            },
        )
        by_id = {r.source_id: r for r in report.sources}
        assert by_id["alpha"].outcome is FetchOutcome.ERROR
        assert "timeout" in (by_id["alpha"].error or "").lower()
        assert by_id["beta"].ok

    @pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
    def test_http_errors_are_recorded_with_their_status(
        self, connection: sqlite3.Connection, status: int
    ) -> None:
        report = run(
            connection, {"alpha.invalid": httpx.Response(status)}, cfg=config(only_alpha=True)
        )
        assert report.sources[0].outcome is FetchOutcome.ERROR
        assert report.sources[0].http_status == status

    def test_malformed_feed_is_reported(self, connection: sqlite3.Connection) -> None:
        report = run(
            connection,
            {"alpha.invalid": rss_response("truncated.xml")},
            cfg=config(only_alpha=True),
        )
        assert report.sources[0].outcome is FetchOutcome.ERROR
        assert "parse" in (report.sources[0].error or "")

    def test_failures_are_never_silently_swallowed(
        self, connection: sqlite3.Connection
    ) -> None:
        report = run(
            connection, {"alpha.invalid": httpx.Response(500)}, cfg=config(only_alpha=True)
        )
        assert report.sources[0].error
        assert not report.all_ok

    def test_failure_state_is_persisted_and_counted(
        self, connection: sqlite3.Connection
    ) -> None:
        for _ in range(2):
            run(connection, {"alpha.invalid": httpx.Response(500)}, cfg=config(only_alpha=True))
        state = SourceFetchStateRepository(connection).get("alpha")
        assert state.last_outcome is FetchOutcome.ERROR
        assert state.consecutive_failures == 2
        assert state.last_error

    def test_recovery_resets_the_failure_counter(self, connection: sqlite3.Connection) -> None:
        run(connection, {"alpha.invalid": httpx.Response(500)}, cfg=config(only_alpha=True))
        run(connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True))

        state = SourceFetchStateRepository(connection).get("alpha")
        assert state.consecutive_failures == 0
        assert state.last_outcome is FetchOutcome.OK
        assert state.last_error is None

    def test_a_failure_keeps_the_previous_validators(
        self, connection: sqlite3.Connection
    ) -> None:
        """A transient outage must not force a full re-download once the source returns."""
        run(
            connection,
            {"alpha.invalid": rss_response(etag='W/"v1"')},
            cfg=config(only_alpha=True),
        )
        run(connection, {"alpha.invalid": httpx.Response(503)}, cfg=config(only_alpha=True))
        assert SourceFetchStateRepository(connection).get("alpha").etag == 'W/"v1"'


class TestSourceSelection:
    def test_collects_only_the_named_source(self, connection: sqlite3.Connection) -> None:
        report = run(
            connection,
            {"alpha.invalid": rss_response(), "beta.invalid": rss_response()},
            source_ids=["beta"],
        )
        assert [r.source_id for r in report.sources] == ["beta"]

    def test_unknown_source_id_fails_clearly(self, connection: sqlite3.Connection) -> None:
        from ai_news_editor.domain.errors import ConfigurationError

        with pytest.raises(ConfigurationError, match="unknown source id"):
            run(connection, {"alpha.invalid": rss_response()}, source_ids=["ghost"])

    def test_disabled_sources_are_skipped_by_default(
        self, connection: sqlite3.Connection
    ) -> None:
        data = {
            **TWO_SOURCES,
            "sources": [TWO_SOURCES["sources"][0], {**TWO_SOURCES["sources"][1], "enabled": False}],
        }
        report = run(
            connection,
            {"alpha.invalid": rss_response()},
            cfg=SourcesConfig.model_validate(data),
        )
        assert [r.source_id for r in report.sources] == ["alpha"]

    def test_naming_a_disabled_source_collects_it_anyway(
        self, connection: sqlite3.Connection
    ) -> None:
        """An explicit request by id is a clearer signal of intent than the config default."""
        data = {
            **TWO_SOURCES,
            "sources": [TWO_SOURCES["sources"][0], {**TWO_SOURCES["sources"][1], "enabled": False}],
        }
        report = run(
            connection,
            {"beta.invalid": rss_response()},
            cfg=SourcesConfig.model_validate(data),
            source_ids=["beta"],
        )
        assert [r.source_id for r in report.sources] == ["beta"]
        assert report.sources[0].inserted == 2


class TestDryRun:
    def test_writes_no_raw_items(self, connection: sqlite3.Connection) -> None:
        run(
            connection,
            {"alpha.invalid": rss_response()},
            cfg=config(only_alpha=True),
            dry_run=True,
        )
        assert RawItemRepository(connection).count() == 0

    def test_writes_no_fetch_state(self, connection: sqlite3.Connection) -> None:
        """A dry run must not change what the next real run does."""
        run(
            connection,
            {"alpha.invalid": rss_response(etag='W/"v1"')},
            cfg=config(only_alpha=True),
            dry_run=True,
        )
        assert SourceFetchStateRepository(connection).find("alpha") is None

    def test_writes_no_sources(self, connection: sqlite3.Connection) -> None:
        run(
            connection,
            {"alpha.invalid": rss_response()},
            cfg=config(only_alpha=True),
            dry_run=True,
        )
        assert SourceRepository(connection).count() == 0

    def test_reports_what_would_be_inserted(self, connection: sqlite3.Connection) -> None:
        report = run(
            connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True), dry_run=True
        )
        assert report.dry_run
        assert report.sources[0].fetched == 3
        assert report.sources[0].inserted == 2

    def test_accounts_for_items_already_stored(self, connection: sqlite3.Connection) -> None:
        run(connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True))
        report = run(
            connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True), dry_run=True
        )
        assert report.sources[0].inserted == 0
        assert report.sources[0].existing == 3

    def test_a_real_run_after_a_dry_run_still_inserts(
        self, connection: sqlite3.Connection
    ) -> None:
        run(
            connection,
            {"alpha.invalid": rss_response()},
            cfg=config(only_alpha=True),
            dry_run=True,
        )
        report = run(connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True))
        assert report.inserted == 2


class TestReportTotals:
    def test_totals_aggregate_across_sources(self, connection: sqlite3.Connection) -> None:
        report = run(
            connection,
            {"alpha.invalid": rss_response(), "beta.invalid": rss_response("atom_full.xml")},
        )
        assert report.fetched == 5
        assert report.inserted == 4
        assert report.existing == 1
        assert report.run_id == "testrun"

    def test_all_ok_is_false_when_any_source_fails(
        self, connection: sqlite3.Connection
    ) -> None:
        report = run(
            connection,
            {"alpha.invalid": httpx.Response(500), "beta.invalid": rss_response()},
        )
        assert not report.all_ok


class TestNoEditorialProcessing:
    """Phase 2 is ingestion only: nothing downstream may be produced yet."""

    def test_collection_creates_no_articles_or_drafts(
        self, connection: sqlite3.Connection
    ) -> None:
        run(connection, {"alpha.invalid": rss_response(), "beta.invalid": rss_response()})
        for table in ("articles", "drafts", "draft_versions", "review_decisions"):
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM " + table  # noqa: S608 - fixed literal names
            ).fetchone()["n"]
            assert count == 0, f"{table} must stay empty during ingestion"

    def test_stored_text_is_the_source_text_unmodified(
        self, connection: sqlite3.Connection
    ) -> None:
        run(connection, {"alpha.invalid": rss_response()}, cfg=config(only_alpha=True))
        items = RawItemRepository(connection).list_by_source("alpha")
        titles = {item.title_original for item in items}
        assert "Українська новина про штучний інтелект" in titles
