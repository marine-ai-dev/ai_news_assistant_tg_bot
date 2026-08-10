"""The publish command, driven through Typer's runner with scripted input.

The question this file answers: can a person send a post to a real audience by pressing
Enter? The answer must be no, and it must be no for every near-miss — "y", "yes",
"publish" in lower case, an empty line.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.settings import get_settings
from ai_news_editor.storage import db

runner = CliRunner()

CHANNEL = "@test_channel"
TOKEN = "123456789:" + "A" * 35

BODY = (
    "Компанія оновила застосунок: тепер він уміє більше, ніж раніше. Це помітно тим, "
    "хто користується ним щодня — зникає один зайвий крок у роботі."
)

sent: list[dict[str, Any]] = []


def output_of(result: object) -> str:
    import contextlib

    parts = [getattr(result, "output", "") or ""]
    with contextlib.suppress(AttributeError, ValueError):
        parts.append(result.stderr or "")  # type: ignore[attr-defined]
    return " ".join(" ".join(parts).split())


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A temporary database, configured credentials, and a Telegram that cannot exist.

    ``TelegramClient`` is patched to carry a MockTransport, so even a bug that reached
    the network layer would hit this list instead of the internet.
    """
    monkeypatch.setenv("AI_NEWS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AI_NEWS_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("AI_NEWS_TELEGRAM_CHANNEL", CHANNEL)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    sent.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            {"url": str(request.url), **(json.loads(request.content) if request.content else {})}
        )
        if request.url.path.endswith("getMe"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "News",
                                             "username": "test_bot"}},
            )
        if request.url.path.endswith("getChat"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": -100777, "type": "channel",
                                             "title": "Test", "username": "test_channel"}},
            )
        if request.url.path.endswith("getChatMember"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"status": "administrator",
                                             "can_post_messages": True}},
            )
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 555, "chat": {"id": -100777}}}
        )

    from ai_news_editor.publishing import telegram as telegram_module

    original = telegram_module.TelegramClient.__init__

    def patched(self, token, *, transport=None, **kwargs):  # type: ignore[no-untyped-def]
        original(self, token, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(telegram_module.TelegramClient, "__init__", patched)
    monkeypatch.setattr("ai_news_editor.cli.publish.TelegramClient", telegram_module.TelegramClient)

    yield
    get_settings.cache_clear()


def seed(tmp_path: Path, *, approved: bool = True) -> str:
    """One draft, optionally with a real recorded human approval."""
    runner.invoke(app, ["db", "init"])
    connection = db.connect(tmp_path / "ai_news.sqlite3")
    try:
        raw_id, art_id, draft_id, version_id = (str(uuid4()) for _ in range(4))
        connection.execute(
            "INSERT INTO sources (id, name, kind, url, trust_tier, created_at, updated_at) "
            "VALUES ('alpha', 'Alpha Co', 'RSS', 'https://alpha.invalid/f', 'OFFICIAL', "
            "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO raw_items (id, source_id, external_id, title_original, url_original, "
            "payload_raw, content_type, fetched_at) VALUES (?, 'alpha', 'e0', 'S', "
            "'https://alpha.invalid/0', '{}', 'application/rss+xml', "
            "'2026-08-01T00:00:00+00:00')",
            (raw_id,),
        )
        connection.execute(
            "INSERT INTO articles (id, raw_item_id, source_id, title, canonical_url, clean_text, "
            "status, created_at, updated_at) VALUES (?, ?, 'alpha', 'Alpha ships', "
            "'https://alpha.invalid/0', 'Body.', 'NORMALIZED', '2026-08-01T00:00:00+00:00', "
            "'2026-08-01T00:00:00+00:00')",
            (art_id, raw_id),
        )
        connection.execute(
            "INSERT INTO drafts (id, article_id, status, current_version_id, created_at, "
            "updated_at) VALUES (?, ?, 'PENDING_REVIEW', NULL, "
            "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
            (draft_id, art_id),
        )
        connection.execute(
            "INSERT INTO draft_versions (id, draft_id, version_no, title, body, hashtags_json, "
            "category, audience, source_attribution, source_url, post_format, style_version, "
            "writer_notes_json, content_hash, created_by, created_at) VALUES "
            "(?, ?, 1, ?, ?, '[]', 'PRODUCT_UPDATE', 'GENERAL', ?, "
            "'https://alpha.invalid/0', 'STANDARD', '1', '[]', 'placeholder', 'test', "
            "'2026-08-01T00:00:00+00:00')",
            (
                version_id,
                draft_id,
                "🆕 Застосунок отримав нову функцію",
                BODY,
                "🔗 Джерело: Alpha Co\nhttps://alpha.invalid/0",
            ),
        )
        connection.execute(
            "UPDATE drafts SET current_version_id = ? WHERE id = ?", (version_id, draft_id)
        )
    finally:
        connection.close()

    if approved:
        # A real approval through the gate, not a hand-written row.
        from uuid import UUID

        from ai_news_editor.publishing.gate import approve_draft

        connection = db.connect(tmp_path / "ai_news.sqlite3")
        try:
            approve_draft(connection, UUID(draft_id), actor="marina")
        finally:
            connection.close()
    return draft_id


def draft_status(tmp_path: Path) -> str:
    connection = db.connect(tmp_path / "ai_news.sqlite3")
    try:
        return str(connection.execute("SELECT status FROM drafts").fetchone()["status"])
    finally:
        connection.close()


def publications(tmp_path: Path) -> list[sqlite3.Row]:
    connection = db.connect(tmp_path / "ai_news.sqlite3")
    try:
        return list(connection.execute("SELECT * FROM publications"))
    finally:
        connection.close()


def send_calls() -> list[dict[str, Any]]:
    return [call for call in sent if call["url"].endswith("sendMessage")]


class TestConfirmation:
    """The most consequential keystroke in the product, after APPROVE."""

    def test_a_bare_enter_does_not_publish(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["publish", draft_id], input="\n")

        assert send_calls() == []
        assert draft_status(tmp_path) == "APPROVED"
        assert publications(tmp_path) == []

    @pytest.mark.parametrize("typed", ["y", "yes", "YES", "publish", "Publish", "ok", "PUBLIS"])
    def test_anything_other_than_the_word_does_not_publish(
        self, tmp_path: Path, typed: str
    ) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["publish", draft_id], input=f"{typed}\n")

        assert send_calls() == [], f"{typed!r} must not publish"
        assert draft_status(tmp_path) == "APPROVED"

    def test_the_exact_word_publishes(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")

        assert result.exit_code == 0, output_of(result)
        assert len(send_calls()) == 1
        assert draft_status(tmp_path) == "PUBLISHED"

    def test_declining_says_nothing_was_sent(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        output = output_of(runner.invoke(app, ["publish", draft_id], input="no\n"))
        assert "Not published" in output
        assert "Nothing was sent" in output


class TestPreview:
    def test_the_destination_is_shown_before_the_prompt(self, tmp_path: Path) -> None:
        """Never a silent destination. The channel is on screen before the question."""
        draft_id = seed(tmp_path)
        output = output_of(runner.invoke(app, ["publish", draft_id], input="\n"))

        assert CHANNEL in output
        assert output.index(CHANNEL) < output.index("PUBLISH to publish")

    def test_the_final_preview_shows_the_post(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        output = output_of(runner.invoke(app, ["publish", draft_id], input="\n"))

        assert "FINAL PREVIEW" in output
        assert "EXACTLY WHAT WILL BE SENT" in output
        assert "Застосунок отримав нову функцію" in output

    def test_the_preview_names_the_version_and_who_approved_it(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        output = output_of(runner.invoke(app, ["publish", draft_id], input="\n"))
        assert "marina" in output

    def test_the_prompt_says_it_cannot_be_unsent(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        output = output_of(runner.invoke(app, ["publish", draft_id], input="\n"))
        assert "cannot be unsent" in output


class TestUnapprovedThroughTheCli:
    def test_a_pending_draft_cannot_be_published(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path, approved=False)
        result = runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")

        assert result.exit_code == 1
        assert send_calls() == []
        assert "Not publishable" in output_of(result)

    def test_an_unknown_draft_is_rejected(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["publish", str(uuid4())], input="PUBLISH\n")
        assert result.exit_code != 0
        assert send_calls() == []

    def test_a_malformed_id_is_rejected(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["publish", "not-a-uuid"], input="PUBLISH\n")
        assert result.exit_code == 2
        assert send_calls() == []

    def test_no_draft_id_publishes_nothing(self, tmp_path: Path) -> None:
        """The command has a required argument; there is no "publish whatever's next"."""
        seed(tmp_path)
        result = runner.invoke(app, ["publish"], input="PUBLISH\n")
        assert result.exit_code != 0
        assert send_calls() == []


class TestDryRun:
    def test_a_dry_run_shows_the_payload_and_sends_nothing(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["publish", draft_id, "--dry-run"])

        assert result.exit_code == 0
        assert send_calls() == []
        assert "Dry run" in output_of(result)
        assert "FINAL PREVIEW" in output_of(result)

    def test_a_dry_run_never_asks_for_confirmation(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        output = output_of(runner.invoke(app, [ "publish", draft_id, "--dry-run"]))
        assert "PUBLISH to publish" not in output

    def test_a_dry_run_records_no_publication(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["publish", draft_id, "--dry-run"])
        assert publications(tmp_path) == []
        assert draft_status(tmp_path) == "APPROVED"

    def test_a_dry_run_of_an_unapproved_draft_fails(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path, approved=False)
        result = runner.invoke(app, ["publish", draft_id, "--dry-run"])
        assert result.exit_code == 1
        assert send_calls() == []


class TestIdempotencyThroughTheCli:
    def test_running_publish_twice_sends_once(self, tmp_path: Path) -> None:
        """The accident this is for: up-arrow, Enter, in the same terminal."""
        draft_id = seed(tmp_path)
        runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")
        assert len(send_calls()) == 1

        result = runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")
        assert len(send_calls()) == 1, "the channel must not receive a duplicate"
        assert result.exit_code == 1
        assert "Not publishable" in output_of(result)

    def test_the_publication_is_recorded_with_the_message_id(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")

        rows = publications(tmp_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "SUCCEEDED"
        assert rows[0]["message_id"] == 555


class TestNoBypassInTheCli:
    @pytest.mark.parametrize(
        "flag", ["--all", "--publish-all", "--yes", "-y", "--auto", "--score-threshold"]
    )
    def test_bulk_and_auto_flags_do_not_exist(self, tmp_path: Path, flag: str) -> None:
        draft_id = seed(tmp_path)
        result = runner.invoke(app, ["publish", draft_id, flag], input="PUBLISH\n")

        assert result.exit_code != 0
        assert send_calls() == []

    def test_no_registered_option_offers_bulk_publishing(self) -> None:
        """Walk Typer's own registration rather than grepping for words in comments.

        Scoped to the publishing commands: --force on an *editorial export* is a
        different thing entirely, and a test that conflated them would be noise.
        """
        import typer.main

        forbidden = {
            "--all", "--publish-all", "--yes", "-y", "--auto", "--auto-publish",
            "--force", "--no-confirm", "--score-threshold", "--batch",
        }
        command = typer.main.get_command(app)
        found: set[str] = set()

        def walk(cmd: object) -> None:
            for param in getattr(cmd, "params", []):
                found.update(getattr(param, "opts", []))
                found.update(getattr(param, "secondary_opts", []))
            for sub in getattr(cmd, "commands", {}).values():
                walk(sub)

        for name in ("publish", "publication", "telegram"):
            walk(command.commands[name])  # type: ignore[attr-defined]
        assert forbidden & found == set()

    def test_publish_requires_a_named_draft(self) -> None:
        """No search, no 'next one', no queue. One id, typed by a human."""
        import typer.main

        command = typer.main.get_command(app)
        publish_cmd = command.commands["publish"]  # type: ignore[attr-defined]
        required = [p for p in publish_cmd.params if getattr(p, "required", False)]
        assert [p.name for p in required] == ["draft_id"]


class TestPublicationList:
    def test_an_empty_log_says_so(self, tmp_path: Path) -> None:
        seed(tmp_path)
        assert "Nothing has been published" in output_of(
            runner.invoke(app, ["publication", "list"])
        )

    def test_a_success_appears_in_the_log(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")

        output = output_of(runner.invoke(app, ["publication", "list"]))
        assert "SUCCEEDED" in output
        assert "555" in output
        assert CHANNEL in output

    def test_the_log_can_be_filtered_to_one_draft(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")
        output = output_of(runner.invoke(app, ["publication", "list", "--draft", draft_id]))
        assert "SUCCEEDED" in output

    def test_approved_drafts_are_listed_for_publishing(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        output = output_of(runner.invoke(app, ["publication", "approved"]))
        assert draft_id in output

    def test_nothing_approved_says_so(self, tmp_path: Path) -> None:
        seed(tmp_path, approved=False)
        assert "No approved drafts" in output_of(runner.invoke(app, ["publication", "approved"]))


class TestTelegramDoctor:
    def test_doctor_reports_a_healthy_setup(self, tmp_path: Path) -> None:
        seed(tmp_path)
        result = runner.invoke(app, ["telegram", "doctor"])

        assert result.exit_code == 0
        output = output_of(result)
        assert "test_bot" in output
        assert "OK" in output

    def test_doctor_sends_no_message(self, tmp_path: Path) -> None:
        """A diagnostic that posts is not a diagnostic."""
        seed(tmp_path)
        runner.invoke(app, ["telegram", "doctor"])

        assert send_calls() == []
        assert {call["url"].rsplit("/", 1)[-1] for call in sent} <= {
            "getMe", "getChat", "getChatMember"
        }

    def test_doctor_says_nothing_was_sent(self, tmp_path: Path) -> None:
        seed(tmp_path)
        assert "Nothing was sent" in output_of(runner.invoke(app, ["telegram", "doctor"]))

    def test_doctor_shows_the_destination(self, tmp_path: Path) -> None:
        seed(tmp_path)
        assert CHANNEL in output_of(runner.invoke(app, ["telegram", "doctor"]))

    def test_missing_configuration_is_explained_not_invented(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AI_NEWS_TELEGRAM_BOT_TOKEN")
        get_settings.cache_clear()
        result = runner.invoke(app, ["telegram", "doctor"])

        assert result.exit_code == 2
        assert "AI_NEWS_TELEGRAM_BOT_TOKEN" in output_of(result)
        assert "BotFather" in output_of(result)

    def test_publishing_without_configuration_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        draft_id = seed(tmp_path)
        monkeypatch.delenv("AI_NEWS_TELEGRAM_CHANNEL")
        get_settings.cache_clear()

        result = runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")
        assert result.exit_code == 2
        assert send_calls() == []


class TestTokenNeverPrinted:
    def test_the_token_is_not_in_the_publish_output(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        output = output_of(runner.invoke(app, ["publish", draft_id], input="PUBLISH\n"))
        assert TOKEN not in output
        assert "api.telegram.org/bot" not in output

    def test_the_token_is_not_in_the_doctor_output(self, tmp_path: Path) -> None:
        seed(tmp_path)
        assert TOKEN not in output_of(runner.invoke(app, ["telegram", "doctor"]))

    def test_the_token_is_not_stored_in_the_database(self, tmp_path: Path) -> None:
        draft_id = seed(tmp_path)
        runner.invoke(app, ["publish", draft_id], input="PUBLISH\n")

        dump = (tmp_path / "ai_news.sqlite3").read_bytes()
        assert TOKEN.encode() not in dump
