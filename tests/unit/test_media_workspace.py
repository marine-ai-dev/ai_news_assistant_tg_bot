"""media.workspace.MediaWorkspace — Step 4 sections 6 and 30: guaranteed cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_news_editor.media.workspace import MediaWorkspace


class TestWorkspaceLifecycle:
    def test_the_root_exists_only_inside_the_with_block(self, tmp_path: Path) -> None:
        with MediaWorkspace("draft-1", root=tmp_path) as workspace:
            assert workspace.root.is_dir()
            root = workspace.root
        assert not root.exists()

    def test_two_workspaces_never_collide(self, tmp_path: Path) -> None:
        with MediaWorkspace("draft-1", root=tmp_path) as a, MediaWorkspace(
            "draft-1", root=tmp_path
        ) as b:
            assert a.root != b.root

    def test_cleanup_removes_files_written_inside_it(self, tmp_path: Path) -> None:
        with MediaWorkspace("draft-1", root=tmp_path) as workspace:
            (workspace.path("original.jpg")).write_bytes(b"fake-bytes")
            root = workspace.root
            assert (root / "original.jpg").exists()
        assert not root.exists()


class TestCleanupOnException:
    def test_an_exception_inside_the_block_still_cleans_up(self, tmp_path: Path) -> None:
        root_seen: Path | None = None
        with pytest.raises(RuntimeError, match="boom"), MediaWorkspace(
            "draft-1", root=tmp_path
        ) as workspace:
            root_seen = workspace.root
            (workspace.path("original.jpg")).write_bytes(b"x")
            raise RuntimeError("boom")
        assert root_seen is not None
        assert not root_seen.exists()

    def test_cleanup_is_safe_to_call_twice(self, tmp_path: Path) -> None:
        workspace = MediaWorkspace("draft-1", root=tmp_path)
        with workspace:
            pass
        workspace.cleanup()  # must not raise even though the directory is already gone


class TestPathSafety:
    def test_path_rejects_absolute_input(self, tmp_path: Path) -> None:
        with MediaWorkspace("draft-1", root=tmp_path) as workspace, pytest.raises(
            ValueError, match="not a safe workspace filename"
        ):
            workspace.path("/etc/passwd")

    def test_path_rejects_a_parent_traversal_component(self, tmp_path: Path) -> None:
        with MediaWorkspace("draft-1", root=tmp_path) as workspace, pytest.raises(
            ValueError, match="not a safe workspace filename"
        ):
            workspace.path("../../etc/passwd")

    def test_path_rejects_a_nested_directory_component(self, tmp_path: Path) -> None:
        with MediaWorkspace("draft-1", root=tmp_path) as workspace, pytest.raises(
            ValueError, match="not a safe workspace filename"
        ):
            workspace.path("sub/original.jpg")

    def test_path_accepts_a_plain_filename(self, tmp_path: Path) -> None:
        with MediaWorkspace("draft-1", root=tmp_path) as workspace:
            path = workspace.path("processed.jpg")
            assert path.parent == workspace.root


class TestLabelSanitisation:
    def test_a_label_with_unsafe_characters_does_not_break_directory_creation(
        self, tmp_path: Path
    ) -> None:
        with MediaWorkspace("../../weird label!!", root=tmp_path) as workspace:
            assert workspace.root.is_dir()
            assert workspace.root.parent == tmp_path

    def test_no_label_still_produces_a_unique_workspace(self, tmp_path: Path) -> None:
        with MediaWorkspace(root=tmp_path) as workspace:
            assert workspace.root.is_dir()


class TestDefaultRoot:
    def test_runner_temp_env_var_is_used_when_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
        with MediaWorkspace("draft-1") as workspace:
            assert workspace.root.is_relative_to(tmp_path / "ai-news-media")

    def test_without_runner_temp_it_falls_back_to_the_os_temp_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RUNNER_TEMP", raising=False)
        import tempfile

        with MediaWorkspace("draft-1") as workspace:
            assert workspace.root.is_relative_to(Path(tempfile.gettempdir()) / "ai-news-media")
        # __exit__ already removed it — nothing left outside tmp_path to clean up.
