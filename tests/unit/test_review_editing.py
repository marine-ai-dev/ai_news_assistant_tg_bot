"""Editor integration, driven by fake editor scripts rather than a real terminal."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from ai_news_editor.review.editing import (
    EditorError,
    edit_text,
    parse_edited,
    render_editable,
    resolve_editor,
)

HEADLINE = "🆕 Оригінальний заголовок"
BODY = "Оригінальний текст допису.\n\nДругий абзац."


def fake_editor(tmp_path: Path, script: str) -> list[str]:
    """A Python script standing in for $EDITOR. Never opens a terminal."""
    path = tmp_path / "fake_editor.py"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return [sys.executable, str(path)]


REWRITE = """\
import sys, pathlib
target = pathlib.Path(sys.argv[1])
target.write_text({new!r}, encoding="utf-8")
"""

LEAVE_ALONE = "import sys\n"

FAIL = "import sys\nsys.exit(1)\n"


class TestBuffer:
    def test_the_buffer_shows_headline_and_body(self) -> None:
        text = render_editable(HEADLINE, BODY)
        assert HEADLINE in text
        assert "Другий абзац." in text

    def test_the_buffer_explains_what_cannot_be_edited(self) -> None:
        text = render_editable(HEADLINE, BODY)
        assert "cannot be changed here" in text
        assert "new version" in text

    def test_comment_lines_are_stripped_on_parse(self) -> None:
        headline, body = parse_edited(render_editable(HEADLINE, BODY))
        assert headline == HEADLINE
        assert body == BODY

    def test_a_buffer_with_no_headline_is_refused(self) -> None:
        with pytest.raises(EditorError, match="no headline"):
            parse_edited("# only comments\n#\n")

    def test_round_trip_preserves_ukrainian_and_emoji(self) -> None:
        headline = "🤯 Це — «неочікувано»: комп'ютер відповів"
        body = "Текст із апострофом, тире — і емодзі 🙂."
        parsed_headline, parsed_body = parse_edited(render_editable(headline, body))
        assert parsed_headline == headline
        assert parsed_body == body


class TestEditorResolution:
    def test_editor_env_var_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EDITOR", "nano")
        monkeypatch.delenv("VISUAL", raising=False)
        assert resolve_editor() == ["nano"]

    def test_visual_wins_over_editor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EDITOR", "nano")
        monkeypatch.setenv("VISUAL", "vim")
        assert resolve_editor() == ["vim"]

    def test_editor_arguments_are_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """$EDITOR may carry flags; they are split the way a shell would."""
        monkeypatch.setenv("EDITOR", "code -w")
        monkeypatch.delenv("VISUAL", raising=False)
        assert resolve_editor() == ["code", "-w"]

    def test_a_blank_editor_variable_falls_through_to_a_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EDITOR", "   ")
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        assert resolve_editor() == ["nano"]

    def test_a_fallback_editor_is_used_when_nothing_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A first run with no $EDITOR should still open something."""
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.delenv("VISUAL", raising=False)
        only_vim = lambda name: f"/usr/bin/{name}" if name == "vim" else None  # noqa: E731
        monkeypatch.setattr("shutil.which", only_vim)
        assert resolve_editor() == ["vim"]

    def test_a_missing_editor_explains_how_to_set_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(EditorError, match="export EDITOR"):
            resolve_editor()


class TestEditing:
    def test_a_successful_edit_returns_the_new_text(self, tmp_path: Path) -> None:
        new = "# comment\n🆕 Новий заголовок\n\nНовий текст.\n"
        result = edit_text(HEADLINE, BODY, editor=fake_editor(tmp_path, REWRITE.format(new=new)))

        assert result.changed is True
        assert result.headline == "🆕 Новий заголовок"
        assert result.body == "Новий текст."

    def test_no_changes_is_reported_as_unchanged(self, tmp_path: Path) -> None:
        result = edit_text(HEADLINE, BODY, editor=fake_editor(tmp_path, LEAVE_ALONE))
        assert result.changed is False
        assert result.headline == HEADLINE

    def test_a_failing_editor_raises_and_saves_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(EditorError, match="exited with status 1"):
            edit_text(HEADLINE, BODY, editor=fake_editor(tmp_path, FAIL))

    def test_a_missing_editor_binary_is_reported(self) -> None:
        with pytest.raises(EditorError, match="could not run editor"):
            edit_text(HEADLINE, BODY, editor=["/nonexistent/editor-binary"])

    def test_an_emptied_buffer_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(EditorError, match="no headline"):
            edit_text(HEADLINE, BODY, editor=fake_editor(tmp_path, REWRITE.format(new="\n\n")))

    def test_the_temporary_file_is_removed(self, tmp_path: Path) -> None:
        import tempfile

        before = set(Path(tempfile.gettempdir()).glob("ai-news-draft-*"))
        edit_text(HEADLINE, BODY, editor=fake_editor(tmp_path, LEAVE_ALONE))
        after = set(Path(tempfile.gettempdir()).glob("ai-news-draft-*"))
        assert after == before

    def test_the_temporary_file_is_removed_even_when_the_editor_fails(
        self, tmp_path: Path
    ) -> None:
        import tempfile

        before = set(Path(tempfile.gettempdir()).glob("ai-news-draft-*"))
        with pytest.raises(EditorError):
            edit_text(HEADLINE, BODY, editor=fake_editor(tmp_path, FAIL))
        assert set(Path(tempfile.gettempdir()).glob("ai-news-draft-*")) == before


class TestCommandInjection:
    def test_draft_text_is_never_interpolated_into_a_command(self, tmp_path: Path) -> None:
        """Post text arrives from the internet. It must never reach a shell.

        The editor is launched with an argument list and the text goes through a file,
        so a headline containing shell metacharacters is inert.
        """
        marker = tmp_path / "pwned"
        hostile_headline = f"; touch {marker}; echo "
        captured = tmp_path / "captured.txt"

        script = (
            "import sys, pathlib, shutil\n"
            f"shutil.copy(sys.argv[1], {str(captured)!r})\n"
        )
        edit_text(hostile_headline, BODY, editor=fake_editor(tmp_path, script))

        assert not marker.exists(), "shell metacharacters in the headline must not execute"
        assert hostile_headline.strip() in captured.read_text(encoding="utf-8")

    def test_the_editor_is_run_without_a_shell(self) -> None:
        """A shell=True call here would turn any draft into a command."""
        source = Path(
            __import__("ai_news_editor.review.editing", fromlist=["editing"]).__file__
        ).read_text(encoding="utf-8")
        assert "shell=True" not in source
        assert "os.system" not in source


class TestTempFileHygiene:
    def test_the_buffer_is_utf8(self, tmp_path: Path) -> None:
        captured = tmp_path / "captured.txt"
        script = (
            "import sys, pathlib, shutil\n"
            f"shutil.copy(sys.argv[1], {str(captured)!r})\n"
        )
        edit_text("🆕 Заголовок", "Текст із емодзі 🤖.", editor=fake_editor(tmp_path, script))
        assert "🤖" in captured.read_text(encoding="utf-8")

    def test_the_buffer_contains_no_secrets_or_ids(self, tmp_path: Path) -> None:
        """Only editorial text goes into the file the editor sees."""
        text = render_editable(HEADLINE, BODY)
        for forbidden in ("draft_id", "article_id", "evaluation_id", "content_hash"):
            assert forbidden not in text
