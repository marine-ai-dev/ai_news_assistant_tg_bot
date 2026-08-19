"""Temporary media storage with guaranteed cleanup — Step 4 (AI News Agent v2).

Every file this pipeline ever writes — the original download, a processed photo or
video, a thumbnail — lives under one per-publication directory and nowhere else: never
in ``automation_state``, never in SQLite, never committed, never uploaded as a CI
artifact. ``MediaWorkspace`` is a context manager so that guarantee holds regardless of
how the ``with`` block exits — success, a Telegram failure, a processing exception, or
a candidate rejected outright all remove the directory the same way, in ``finally``.

Root directory: ``$RUNNER_TEMP/ai-news-media/`` on a GitHub-hosted runner (`RUNNER_TEMP`
is the runner's own per-job scratch directory, already cleaned up by GitHub between
jobs — this application does not rely on that alone; see the module docstring above).
Falls back to the OS temp directory when ``RUNNER_TEMP`` is unset, i.e. every local
development and test run.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from types import TracebackType

from ai_news_editor.observability.logging import get_logger

logger = get_logger(__name__)

WORKSPACE_DIR_NAME = "ai-news-media"


def _workspace_root() -> Path:
    """``$RUNNER_TEMP/ai-news-media`` if set, else the OS temp dir's own subfolder."""
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(base) / WORKSPACE_DIR_NAME


class MediaWorkspace:
    """A unique, per-publication scratch directory that always removes itself.

    Usage::

        with MediaWorkspace(draft_id) as workspace:
            original = workspace.path("original.jpg")
            ...
        # workspace.root no longer exists, whether the block raised or not.

    ``label`` is folded into the directory name only for a human reading a stray
    listing during debugging — never trusted as a safe path component on its own, since
    it may originate from a draft id this process did not generate.
    """

    def __init__(self, label: str | None = None, *, root: Path | None = None) -> None:
        safe_label = _sanitize_label(label) if label else None
        name = f"{safe_label}-{uuid.uuid4().hex[:12]}" if safe_label else uuid.uuid4().hex
        self._root = (root or _workspace_root()) / name

    @property
    def root(self) -> Path:
        return self._root

    def path(self, filename: str) -> Path:
        """A path for ``filename`` inside this workspace.

        Raises:
            ValueError: ``filename`` is not a plain filename — no path traversal, no
                absolute path, no directory component. This workspace only ever writes
                files it names itself (``original.jpg``, ``processed.mp4``, ...), so a
                caller passing anything else is a bug, not a legitimate use.
        """
        candidate = Path(filename)
        if candidate.is_absolute() or candidate.name != filename or filename in {"", ".", ".."}:
            raise ValueError(f"not a safe workspace filename: {filename!r}")
        return self._root / filename

    def __enter__(self) -> MediaWorkspace:
        self._root.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Remove the workspace directory and everything in it. Safe to call more
        than once — a directory that is already gone is not an error."""
        removed = self._root.exists()
        shutil.rmtree(self._root, ignore_errors=True)
        logger.info(
            "media_cleanup",
            extra={"files_removed": removed, "success": not self._root.exists()},
        )


def _sanitize_label(label: str) -> str:
    """A filesystem-safe fragment for a directory name — never trusted path input."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)
    return safe[:40] or "workspace"


__all__ = ["WORKSPACE_DIR_NAME", "MediaWorkspace"]
