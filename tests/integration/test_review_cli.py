"""The interactive review loop, driven through Typer's runner with scripted input."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.settings import get_settings
from ai_news_editor.storage import db

runner = CliRunner()

BODY = (
    "Компанія оновила застосунок: тепер він уміє більше, ніж раніше. Це помітно тим, "
    "хто користується ним щодня — зникає один зайвий крок у роботі."
)


def output_of(result: object) -> str:
    import contextlib

    parts = [getattr(result, "output", "") or ""]
    with contextlib.suppress(AttributeError, ValueError):
        parts.append(result.stderr or "")  # type: ignore[attr-defined]
    return " ".join(" ".join(parts).split())


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AI_NEWS_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def seed(tmp_path: Path, count: int = 2) -> list[str]:
    """Articles, evaluations and PENDING_REVIEW drafts, straight into the database."""
    runner.invoke(app, ["db", "init"])
    connection = db.connect(tmp_path / "ai_news.sqlite3")
    draft_ids: list[str] = []
    try:
        connection.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
            "VALUES ('alpha', 'Alpha Co', 'RSS', 'https://alpha.invalid/f', 'OFFICIAL', "
            "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
        )
        for i in range(count):
            raw_id, art_id, draft_id, version_id = (str(uuid4()) for _ in range(4))
            draft_ids.append(draft_id)
            connection.execute(
                "INSERT INTO raw_items (id, source_id, external_id, title_original, "
                "url_original, payload_raw, content_type, fetched_at) VALUES "
                "(?, 'alpha', ?, ?, ?, '{}', 'application/rss+xml', '2026-08-01T00:00:00+00:00')",
                (raw_id, f"e{i}", f"Story {i}", f"https://alpha.invalid/{i}"),
            )
            connection.execute(
                "INSERT INTO articles (id, raw_item_id, source_id, title, canonical_url, "
                "clean_text, status, created_at, updated_at) VALUES "
                "(?, ?, 'alpha', ?, ?, 'Body text.', 'NORMALIZED', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
                (art_id, raw_id, f"Alpha ships feature {i}", f"https://alpha.invalid/{i}"),
            )
            # Draft first with a null pointer, then the version, then link them —
            # the same order the repository uses, because the FK is real.
            connection.execute(
                "INSERT INTO drafts (id, article_id, status, current_version_id, "
                "created_at, updated_at) VALUES (?, ?, 'PENDING_REVIEW', NULL, "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
                (draft_id, art_id),
            )
            connection.execute(
                "INSERT INTO draft_versions (id, draft_id, version_no, title, body, "
                "hashtags_json, category, audience, source_attribution, source_url, "
                "post_format, style_version, writer_notes_json, content_hash, created_by, "
                "created_at) VALUES (?, ?, 1, ?, ?, '[]', 'PRODUCT_UPDATE', 'GENERAL', ?, ?, "
                "'STANDARD', '1', '[\"перевірити доступність\"]', 'placeholder', 'test', "
                "'2026-08-01T00:00:00+00:00')",
                (
                    version_id,
                    draft_id,
                    f"🆕 Застосунок отримав функцію {i}",
                    BODY,
                    f"🔗 Джерело: Alpha Co\nhttps://alpha.invalid/{i}",
                    f"https://alpha.invalid/{i}",
                ),
            )
            connection.execute(
                "UPDATE drafts SET current_version_id = ? WHERE id = ?", (version_id, draft_id)
            )
    finally:
        connection.close()
    return draft_ids


def statuses(tmp_path: Path) -> list[str]:
    connection = db.connect(tmp_path / "ai_news.sqlite3")
    try:
        return [row["status"] for row in connection.execute("SELECT status FROM drafts")]
    finally:
        connection.close()


def decisions(tmp_path: Path) -> list[str]:
    connection = db.connect(tmp_path / "ai_news.sqlite3")
    try:
        return [
            row["action"]
            for row in connection.execute("SELECT action FROM review_decisions ORDER BY created_at")
        ]
    finally:
        connection.close()


class TestReviewScreen:
    def test_the_screen_shows_the_post_and_metadata(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        result = runner.invoke(app, ["review"], input="q\n")
        output = output_of(result)

        assert "DRAFT 1 / 1" in output
        assert "TELEGRAM PREVIEW" in output
        assert "Застосунок отримав функцію" in output
        assert "PRODUCT_UPDATE" in output
        assert "перевірити доступність" in output

    def test_the_actions_are_offered(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        output = output_of(runner.invoke(app, ["review"], input="q\n"))
        for label in ("Approve", "Edit", "Reject", "Needs rewrite", "Skip", "Quit"):
            assert label in output

    def test_an_empty_queue_says_so(self, tmp_path: Path) -> None:
        runner.invoke(app, ["db", "init"])
        assert "Nothing awaiting review" in output_of(runner.invoke(app, ["review"]))


class TestApprovalConfirmation:
    def test_the_exact_word_approves(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        result = runner.invoke(app, ["review"], input="a\nAPPROVE\n\nq\n")

        assert result.exit_code == 0
        assert statuses(tmp_path) == ["APPROVED"]
        assert decisions(tmp_path) == ["APPROVE"]

    def test_a_bare_enter_does_not_approve(self, tmp_path: Path) -> None:
        """The single most important keystroke in the product."""
        seed(tmp_path, 1)
        runner.invoke(app, ["review"], input="a\n\nq\n")

        assert statuses(tmp_path) == ["PENDING_REVIEW"]
        assert decisions(tmp_path) == []

    @pytest.mark.parametrize("typed", ["approve", "Approve", "y", "yes", "APPROVE ME", "ok"])
    def test_anything_other_than_the_word_cancels(self, tmp_path: Path, typed: str) -> None:
        seed(tmp_path, 1)
        runner.invoke(app, ["review"], input=f"a\n{typed}\nq\n")

        assert statuses(tmp_path) == ["PENDING_REVIEW"]
        assert decisions(tmp_path) == []

    def test_cancelling_says_so(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        assert "Not approved" in output_of(runner.invoke(app, ["review"], input="a\nnope\nq\n"))

    def test_approval_says_it_is_not_published(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        output = output_of(runner.invoke(app, ["review"], input="a\nAPPROVE\n\nq\n"))
        assert "not published" in output

    def test_an_approval_note_is_stored(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        runner.invoke(app, ["review"], input="a\nAPPROVE\nперевірив формулювання\nq\n")

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            note = connection.execute("SELECT note FROM review_decisions").fetchone()["note"]
        finally:
            connection.close()
        assert note == "перевірив формулювання"


class TestRejectAndRewrite:
    def test_reject_records_and_transitions(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        runner.invoke(app, ["review"], input="r\nне для цього каналу\nq\n")

        assert statuses(tmp_path) == ["REJECTED"]
        assert decisions(tmp_path) == ["REJECT"]

    def test_needs_rewrite_records_and_transitions(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        runner.invoke(app, ["review"], input="w\nзанадто технічно\nq\n")

        assert statuses(tmp_path) == ["NEEDS_REWRITE"]
        assert decisions(tmp_path) == ["REQUEST_REWRITE"]

    def test_a_rejected_draft_leaves_the_queue(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        runner.invoke(app, ["review"], input="r\n\nq\n")
        assert "Nothing awaiting review" in output_of(runner.invoke(app, ["review"]))


class TestNavigation:
    def test_skip_changes_nothing(self, tmp_path: Path) -> None:
        seed(tmp_path, 2)
        runner.invoke(app, ["review"], input="s\ns\n")

        assert statuses(tmp_path) == ["PENDING_REVIEW", "PENDING_REVIEW"]
        assert decisions(tmp_path) == []

    def test_next_changes_nothing(self, tmp_path: Path) -> None:
        seed(tmp_path, 2)
        runner.invoke(app, ["review"], input="n\nn\n")
        assert decisions(tmp_path) == []

    def test_quitting_leaves_the_rest_pending(self, tmp_path: Path) -> None:
        seed(tmp_path, 2)
        runner.invoke(app, ["review"], input="a\nAPPROVE\n\nq\n")

        assert sorted(statuses(tmp_path)) == ["APPROVED", "PENDING_REVIEW"]

    def test_a_later_run_resumes_with_what_is_left(self, tmp_path: Path) -> None:
        seed(tmp_path, 2)
        runner.invoke(app, ["review"], input="a\nAPPROVE\n\nq\n")

        second = output_of(runner.invoke(app, ["review"], input="q\n"))
        assert "DRAFT 1 / 1" in second


class TestFilters:
    def test_a_single_draft_can_be_reviewed(self, tmp_path: Path) -> None:
        ids = seed(tmp_path, 2)
        output = output_of(runner.invoke(app, ["review", "--draft", ids[0]], input="q\n"))
        assert "DRAFT 1 / 1" in output

    def test_an_unknown_draft_id_is_rejected(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        result = runner.invoke(app, ["review", "--draft", "not-a-uuid"], input="q\n")
        assert result.exit_code == 2

    def test_a_category_filter_works(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        output = output_of(
            runner.invoke(app, ["review", "--category", "AI_FAIL"], input="q\n")
        )
        assert "Nothing awaiting review" in output


class TestReadOnlyViews:
    def test_status_reports_counts(self, tmp_path: Path) -> None:
        seed(tmp_path, 2)
        result = runner.invoke(app, ["review", "status"])
        assert result.exit_code == 0
        assert "PENDING_REVIEW" in output_of(result)

    def test_status_explains_approved_is_not_published(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        assert "does not mean published" in output_of(runner.invoke(app, ["review", "status"]))

    def test_history_shows_versions_and_decisions(self, tmp_path: Path) -> None:
        ids = seed(tmp_path, 1)
        runner.invoke(app, ["review"], input="a\nAPPROVE\n\nq\n")

        result = runner.invoke(app, ["review", "history", ids[0][:8]])
        assert result.exit_code == 0
        output = output_of(result)
        assert "Version 1" in output
        assert "APPROVE" in output
        assert "authorization is valid" in output

    def test_history_of_a_pending_draft_has_no_authorization(self, tmp_path: Path) -> None:
        ids = seed(tmp_path, 1)
        output = output_of(runner.invoke(app, ["review", "history", ids[0][:8]]))
        assert "No valid publication authorization" in output

    def test_history_rejects_an_unknown_draft(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        result = runner.invoke(app, ["review", "history", "zzzzzzzz"])
        assert result.exit_code == 1


class TestNoBypassInTheCli:
    @pytest.mark.parametrize("flag", ["--yes", "-y", "--approve-all", "--auto-approve"])
    def test_bulk_flags_are_not_accepted(self, tmp_path: Path, flag: str) -> None:
        seed(tmp_path, 1)
        result = runner.invoke(app, ["review", flag], input="q\n")

        assert result.exit_code != 0
        assert statuses(tmp_path) == ["PENDING_REVIEW"]

    def test_there_is_no_approve_command(self, tmp_path: Path) -> None:
        seed(tmp_path, 1)
        for command in (["approve"], ["review", "approve"], ["draft", "approve"]):
            assert runner.invoke(app, command).exit_code != 0
        assert statuses(tmp_path) == ["PENDING_REVIEW"]

    def test_reviewing_without_choosing_approves_nothing(self, tmp_path: Path) -> None:
        """Walking the whole queue with the default action must decide nothing."""
        seed(tmp_path, 2)
        runner.invoke(app, ["review"], input="\n\n")

        assert statuses(tmp_path) == ["PENDING_REVIEW", "PENDING_REVIEW"]
        assert decisions(tmp_path) == []


class TestEditThroughTheCli:
    def test_editing_creates_a_second_version_and_keeps_review_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(tmp_path, 1)

        from ai_news_editor.review.editing import EditResult

        monkeypatch.setattr(
            "ai_news_editor.cli.review.edit_text",
            lambda headline, body: EditResult(
                headline="🆕 Відредагований заголовок", body=BODY, changed=True
            ),
        )
        result = runner.invoke(app, ["review"], input="e\nq\n")

        assert "Saved as version 2" in output_of(result)
        assert statuses(tmp_path) == ["PENDING_REVIEW"]
        assert decisions(tmp_path) == ["EDIT"]

    def test_an_unchanged_edit_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(tmp_path, 1)

        from ai_news_editor.review.editing import EditResult

        monkeypatch.setattr(
            "ai_news_editor.cli.review.edit_text",
            lambda headline, body: EditResult(headline=headline, body=body, changed=False),
        )
        runner.invoke(app, ["review"], input="e\nq\n")
        assert decisions(tmp_path) == []

    def test_an_invalid_edit_is_refused_and_saves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(tmp_path, 1)

        from ai_news_editor.review.editing import EditResult

        monkeypatch.setattr(
            "ai_news_editor.cli.review.edit_text",
            lambda headline, body: EditResult(
                headline="🆕 Заголовок", body="я" * 4000, changed=True
            ),
        )
        result = runner.invoke(app, ["review"], input="e\nq\n")

        assert "Edit rejected" in output_of(result)
        assert decisions(tmp_path) == []

    def test_a_failing_editor_is_reported_and_saves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(tmp_path, 1)

        from ai_news_editor.review.editing import EditorError

        def explode(headline: str, body: str) -> None:
            raise EditorError("editor exited with status 1")

        monkeypatch.setattr("ai_news_editor.cli.review.edit_text", explode)
        result = runner.invoke(app, ["review"], input="e\nq\n")

        assert "Edit cancelled" in output_of(result)
        assert decisions(tmp_path) == []

    def test_editing_after_approval_returns_the_draft_to_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end shape of the invariant, through the real CLI."""
        seed(tmp_path, 1)
        runner.invoke(app, ["review"], input="a\nAPPROVE\n\nq\n")
        assert statuses(tmp_path) == ["APPROVED"]

        from ai_news_editor.review.editing import EditResult

        monkeypatch.setattr(
            "ai_news_editor.cli.review.edit_text",
            lambda headline, body: EditResult(
                headline="🆕 Змінений після схвалення", body=BODY, changed=True
            ),
        )
        # An approved draft is no longer in the pending queue, so target it directly.
        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            connection.execute("UPDATE drafts SET status = 'PENDING_REVIEW'")
        finally:
            connection.close()

        runner.invoke(app, ["review"], input="e\nq\n")

        assert statuses(tmp_path) == ["PENDING_REVIEW"]
        assert decisions(tmp_path) == ["APPROVE", "EDIT"]

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            versions = connection.execute(
                "SELECT version_no FROM draft_versions ORDER BY version_no"
            ).fetchall()
        finally:
            connection.close()
        assert [row["version_no"] for row in versions] == [1, 2]
