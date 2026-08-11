"""The prompt/explainer exchange: template out, batch in, drafts awaiting review."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ai_news_editor.cli.main import app
from ai_news_editor.content.import_items import ContentImportError, import_batch, load_batch
from ai_news_editor.content.schema import CONTENT_SCHEMA_VERSION
from ai_news_editor.domain.enums import ContentType, DraftStatus
from ai_news_editor.settings import get_settings
from ai_news_editor.storage.repositories import ContentItemRepository, DraftRepository
from ai_news_editor.writing.schema import STYLE_VERSION

runner = CliRunner()


def output_of(result: object) -> str:
    """Rich hard-wraps at the terminal width; compare on normalized whitespace."""
    return " ".join((getattr(result, "output", "") or "").split())

PROMPT_TEXT = (
    "Я надішлю список продуктів, які є вдома. Запропонуй три прості страви, які з них "
    "можна приготувати, і напиши, чого не вистачає."
)

PROMPT_POST = (
    "Не завжди хочеться думати, що готувати з того, що є. Це можна перекласти на ШІ.\n\n"
    "📋 Готовий промпт:\n"
    f"{PROMPT_TEXT}\n\n"
    "💡 Як адаптувати: вкажіть, скільки часу у вас є, або попросіть страви без духовки."
)

EXPLAINER_POST = (
    "Промпт — це просто те, що ви пишете ШІ. Не команда й не код: звичайний текст "
    "своїми словами.\n\n"
    "Це схоже на записку колезі. «Зроби звіт» і «зроби короткий звіт по продажах за "
    "травень, у вигляді списку» дадуть дуже різний результат.\n\n"
    "Чим точніше ви скажете, чого хочете, тим кориснішою буде відповідь."
)


def prompt_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "content_type": "PROMPT",
        "title": "Що приготувати з того, що є",
        "audience": "NEWCOMER",
        "topic": "FOOD",
        "what_you_can_do": "швидко вирішити, що приготувати",
        "prompt_text": PROMPT_TEXT,
        "customization_tips": ["вкажіть, скільки часу у вас є"],
        "works_with": None,
        "references": [],
        "post": {
            "headline": "✨ Промпт: що приготувати з того, що вже є вдома",
            "body": PROMPT_POST,
            "category": "EVERYDAY_AI",
            "post_format": "QUICK",
            "hashtags": [],
            "writer_notes": [],
        },
    }
    item.update(overrides)
    return item


def explainer_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "content_type": "EXPLAINER",
        "title": "Що таке промпт",
        "audience": "NEWCOMER",
        "concept": "Промпт",
        "simple_explanation": "Промпт — це те, що ви пишете ШІ.",
        "real_life_example": "Як записка колезі: чим точніше, тим кращий результат.",
        "why_it_matters": "Від формулювання залежить якість відповіді.",
        "try_this": None,
        "references": [],
        "post": {
            "headline": "🧠 Що таке промпт — і чому від нього залежить відповідь",
            "body": EXPLAINER_POST,
            "category": "EXPLAINED_SIMPLY",
            "post_format": "QUICK",
            "hashtags": [],
            "writer_notes": [],
        },
    }
    item.update(overrides)
    return item


def batch_file(tmp_path: Path, *items: dict[str, Any], **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "schema_version": CONTENT_SCHEMA_VERSION,
        "style_version": STYLE_VERSION,
        "batch_id": "content-test",
        "author": "claude-code",
        "items": list(items) or [prompt_item()],
    }
    payload.update(overrides)
    path = tmp_path / "batch.content.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestValidation:
    def test_a_well_formed_batch_loads(self, tmp_path: Path) -> None:
        batch = load_batch(batch_file(tmp_path, prompt_item(), explainer_item()))
        assert [i.content_type for i in batch.items] == [
            ContentType.PROMPT,
            ContentType.EXPLAINER,
        ]

    def test_an_unknown_content_type_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ContentImportError):
            load_batch(batch_file(tmp_path, prompt_item(content_type="HOW_TO")))

    def test_an_unknown_audience_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ContentImportError):
            load_batch(batch_file(tmp_path, prompt_item(audience="EXPERT")))

    def test_an_unknown_topic_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ContentImportError):
            load_batch(batch_file(tmp_path, prompt_item(topic="CRYPTO")))

    def test_a_prompt_missing_from_its_own_post_is_refused(self, tmp_path: Path) -> None:
        """The one thing a prompt post must deliver: something to copy."""
        item = prompt_item()
        item["post"] = {**item["post"], "body": "Дуже корисний промпт. " * 12}
        with pytest.raises(ContentImportError, match="copy"):
            load_batch(batch_file(tmp_path, item))

    def test_a_prompt_without_customization_guidance_is_refused(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ContentImportError):
            load_batch(batch_file(tmp_path, prompt_item(customization_tips=[])))

    def test_an_explainer_missing_its_example_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ContentImportError):
            load_batch(batch_file(tmp_path, explainer_item(real_life_example="")))

    def test_a_stale_style_version_is_refused(self, tmp_path: Path) -> None:
        """A guide revision must not silently reinterpret text written under the old one."""
        with pytest.raises(ContentImportError, match="style_version"):
            load_batch(batch_file(tmp_path, prompt_item(), style_version="1"))

    def test_markup_outside_the_permitted_subset_is_refused(self, tmp_path: Path) -> None:
        item = prompt_item()
        item["post"] = {**item["post"], "body": f"<script>x</script>{PROMPT_POST}"}
        with pytest.raises(ContentImportError):
            load_batch(batch_file(tmp_path, item))

    def test_a_duplicate_item_in_one_batch_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ContentImportError, match="more than once"):
            load_batch(batch_file(tmp_path, prompt_item(), prompt_item()))

    def test_the_schema_cannot_express_approval_or_publication(self) -> None:
        """No field could ask for it, which is why importing cannot produce it."""
        from ai_news_editor.content.schema import ContentBatch, SubmittedPost, SubmittedPrompt

        fields = (
            set(ContentBatch.model_fields)
            | set(SubmittedPrompt.model_fields)
            | set(SubmittedPost.model_fields)
        )
        for forbidden in ("status", "approved", "approve", "publish", "published", "channel"):
            assert not any(forbidden in name for name in fields)


class TestImport:
    def test_importing_creates_content_items_and_drafts(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        outcome = import_batch(
            connection, batch_file(tmp_path, prompt_item(), explainer_item())
        )
        assert outcome.count == 2
        assert ContentItemRepository(connection).count() == 2

    def test_every_imported_draft_awaits_review(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        outcome = import_batch(connection, batch_file(tmp_path, prompt_item()))
        drafts = DraftRepository(connection)
        for draft_id, _title in outcome.created:
            assert drafts.get(draft_id).status is DraftStatus.PENDING_REVIEW

    def test_the_draft_points_at_its_content_item_and_no_article(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        outcome = import_batch(connection, batch_file(tmp_path, explainer_item()))
        draft = DraftRepository(connection).get(outcome.created[0][0])
        assert draft.content_type is ContentType.EXPLAINER
        assert draft.content_item_id is not None
        assert draft.article_id is None

    def test_the_structured_body_survives_the_round_trip(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        import_batch(connection, batch_file(tmp_path, prompt_item()))
        item = ContentItemRepository(connection).list_by_type(ContentType.PROMPT)[0]
        assert item.body.prompt_text == PROMPT_TEXT  # type: ignore[union-attr]
        assert item.body.customization_tips  # type: ignore[union-attr]

    def test_references_round_trip(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        item = explainer_item(
            references=[
                {
                    "label": "OpenAI Help",
                    "url": "https://help.openai.com/x",
                    "supports": "що безкоштовний план має ліміти",
                }
            ]
        )
        import_batch(connection, batch_file(tmp_path, item))
        stored = ContentItemRepository(connection).list_by_type(ContentType.EXPLAINER)[0]
        assert stored.references[0].supports == "що безкоштовний план має ліміти"

    def test_reimporting_the_same_batch_adds_nothing(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        path = batch_file(tmp_path, prompt_item(), explainer_item())
        import_batch(connection, path)
        again = import_batch(connection, path)

        assert again.count == 0
        assert len(again.skipped) == 2
        assert ContentItemRepository(connection).count() == 2

    def test_an_invalid_batch_writes_nothing(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        with pytest.raises(ContentImportError):
            import_batch(connection, batch_file(tmp_path, prompt_item(audience="EXPERT")))
        assert ContentItemRepository(connection).count() == 0

    def test_jargon_warnings_are_reported_not_enforced(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """An unexplained term is a note for the reviewer, never a refusal."""
        item = explainer_item(
            title="Про агентів",
            post={
                **explainer_item()["post"],
                "headline": "🧠 Що вміє AI-агент",
                "body": (
                    "AI-агент виконує завдання замість вас. Ви ставите ціль, і далі він "
                    "робить кроки сам, без вашої участі на кожному етапі. Так працюють "
                    "деякі нові функції у застосунках, якими ви вже користуєтесь щодня."
                ),
            },
        )
        outcome = import_batch(connection, batch_file(tmp_path, item))

        assert outcome.count == 1, "the draft is still created"
        assert any("агент" in note for note in outcome.warnings)

    def test_a_technical_audience_is_not_linted_for_jargon(
        self, connection: sqlite3.Connection, tmp_path: Path
    ) -> None:
        item = explainer_item(
            audience="TECH_CURIOUS",
            post={
                **explainer_item()["post"],
                "body": "Контекстне вікно моделі. " + EXPLAINER_POST,
            },
        )
        assert import_batch(connection, batch_file(tmp_path, item)).warnings == []


class TestContentCli:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        monkeypatch.setenv("AI_NEWS_DATA_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        get_settings.cache_clear()
        runner.invoke(app, ["db", "init"])
        yield
        get_settings.cache_clear()

    def test_the_template_is_valid_shape_and_carries_the_versions(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["content", "template"])
        assert result.exit_code == 0

        written = next((tmp_path / "content_work").glob("*.content.json"))
        payload = json.loads(written.read_text(encoding="utf-8"))
        assert payload["schema_version"] == CONTENT_SCHEMA_VERSION
        assert payload["style_version"] == STYLE_VERSION
        assert {i["content_type"] for i in payload["items"]} == {"PROMPT", "EXPLAINER"}

    def test_validate_reports_problems_without_writing(self, tmp_path: Path) -> None:
        path = batch_file(tmp_path, prompt_item(audience="EXPERT"))
        result = runner.invoke(app, ["content", "validate", str(path)])
        assert result.exit_code == 1
        assert "Invalid" in output_of(result)

    def test_validate_accepts_a_good_batch(self, tmp_path: Path) -> None:
        path = batch_file(tmp_path, prompt_item(), explainer_item())
        result = runner.invoke(app, ["content", "validate", str(path)])
        assert result.exit_code == 0
        assert "Nothing was written" in output_of(result)

    def test_import_creates_drafts_awaiting_review(self, tmp_path: Path) -> None:
        path = batch_file(tmp_path, prompt_item(), explainer_item())
        result = runner.invoke(app, ["content", "import", str(path)])

        assert result.exit_code == 0
        assert "2 draft(s) created" in output_of(result)
        assert "Nothing is approved and nothing is published" in output_of(result)

    def test_import_of_an_invalid_batch_fails_cleanly(self, tmp_path: Path) -> None:
        path = batch_file(tmp_path, prompt_item(topic="CRYPTO"))
        result = runner.invoke(app, ["content", "import", str(path)])
        assert result.exit_code == 1
        assert "Not imported" in output_of(result)

    def test_list_shows_imported_items(self, tmp_path: Path) -> None:
        runner.invoke(app, ["content", "import", str(batch_file(tmp_path, prompt_item()))])
        result = runner.invoke(app, ["content", "list"])
        assert "PROMPT" in output_of(result)

    def test_list_rejects_an_unknown_type(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["content", "list", "--type", "HOW_TO"])
        assert result.exit_code == 2

    def test_the_content_commands_cannot_approve_or_publish(self) -> None:
        import typer.main

        command = typer.main.get_command(app).commands["content"]  # type: ignore[attr-defined]
        names = set(command.commands)  # type: ignore[attr-defined]
        assert not any(
            word in name for name in names for word in ("approve", "publish", "send")
        )


class TestBadInput:
    def test_a_missing_file_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ContentImportError, match="no such file"):
            load_batch(tmp_path / "nope.json")

    def test_a_file_that_is_not_json_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ContentImportError, match="not valid JSON"):
            load_batch(path)

    def test_a_wrong_schema_version_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ContentImportError, match="schema_version"):
            load_batch(batch_file(tmp_path, prompt_item(), schema_version="99"))

    def test_a_headline_over_the_limit_is_refused(self, tmp_path: Path) -> None:
        item = prompt_item()
        item["post"] = {**item["post"], "headline": "я" * 300}
        with pytest.raises(ContentImportError):
            load_batch(batch_file(tmp_path, item))

    def test_a_post_below_the_minimum_length_is_refused(self, tmp_path: Path) -> None:
        item = explainer_item()
        item["post"] = {**item["post"], "body": "Коротко."}
        with pytest.raises(ContentImportError):
            load_batch(batch_file(tmp_path, item))

    def test_the_rendered_preview_is_the_post_text(self, tmp_path: Path) -> None:
        from ai_news_editor.content.import_items import rendered_preview

        batch = load_batch(batch_file(tmp_path, explainer_item()))
        preview = rendered_preview(batch.items[0])
        assert batch.items[0].post.headline in preview
        assert "🔗 Джерело" not in preview, "editorial content carries no source line"

    def test_validate_can_print_each_post(self, tmp_path: Path) -> None:
        path = batch_file(tmp_path, explainer_item())
        result = runner.invoke(app, ["content", "validate", str(path), "--show"])
        assert result.exit_code == 0
        assert "Що таке промпт" in output_of(result)
