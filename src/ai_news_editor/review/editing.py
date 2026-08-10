"""Opening a draft in the operator's editor.

Draft text is written to a temporary file and the editor is launched with argument
lists — never a shell string. Post text comes from the internet by way of an LLM
session; interpolating it into a command line would be handing a stranger a shell.

If the editor fails, or the text comes back unchanged, nothing is written to the
database. A new version is created only when a human actually changed something.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ai_news_editor.domain.errors import AiNewsError

#: Tried in order when $EDITOR and $VISUAL are unset. Present on essentially every mac
#: and Linux box; if none exists the operator is told how to set $EDITOR rather than
#: having some choice made for them.
FALLBACK_EDITORS = ("nano", "vim", "vi")

_HELP_HEADER = """\
# Edit the post below, then save and close.
#
# Lines starting with '#' are ignored.
# The first non-comment line is the HEADLINE; everything after the blank line is the BODY.
#
# The source, category, audience and article link cannot be changed here — an edit
# revises the writing, not what the writing is about.
#
# Saving creates a new version. The previous version is kept and, if it had been
# approved, that approval stops applying and the draft returns for review.
# Quit without saving (or leave the text unchanged) to cancel.
"""


class EditorError(AiNewsError):
    """The editor could not be run, or returned nothing usable."""


@dataclass(frozen=True, slots=True)
class EditResult:
    """What came back from the editor."""

    headline: str
    body: str
    changed: bool


def resolve_editor() -> list[str]:
    """The editor command as an argument list.

    ``$EDITOR`` may legitimately carry arguments (``code -w``), so it is split the way a
    shell would split it — and then executed *without* one.

    Raises:
        EditorError: no editor is configured and no fallback exists.
    """
    configured = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    # Split the way a shell would: $EDITOR often carries flags ("code -w"). A variable
    # set to whitespace splits to nothing and falls through to the fallbacks.
    parts = shlex.split(configured) if configured else []
    if parts:
        return parts

    from shutil import which

    for candidate in FALLBACK_EDITORS:
        if which(candidate):
            return [candidate]

    raise EditorError(
        "no editor found. Set one first, for example:  export EDITOR=nano"
    )


def render_editable(headline: str, body: str) -> str:
    """The buffer the operator sees."""
    return f"{_HELP_HEADER}\n{headline.strip()}\n\n{body.strip()}\n"


def parse_edited(text: str) -> tuple[str, str]:
    """Split an edited buffer back into headline and body.

    Raises:
        EditorError: the buffer has no usable headline.
    """
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        raise EditorError("the edited text has no headline")

    headline = lines[index].strip()
    body = "\n".join(lines[index + 1 :]).strip()
    return headline, body


def edit_text(
    headline: str, body: str, *, editor: list[str] | None = None
) -> EditResult:
    """Open the post in an editor and return what came back.

    Raises:
        EditorError: the editor is missing, exited non-zero, or produced no headline.
    """
    command = editor or resolve_editor()
    original = render_editable(headline, body)

    handle, raw_path = tempfile.mkstemp(prefix="ai-news-draft-", suffix=".md", text=True)
    path = Path(raw_path)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(original)

        try:
            completed = subprocess.run([*command, str(path)], check=False)  # noqa: S603
        except OSError as exc:
            raise EditorError(f"could not run editor {command[0]!r}: {exc}") from exc

        if completed.returncode != 0:
            # A non-zero exit is how editors report ":cq" and crashes alike. Either way
            # the operator did not finish, so nothing is saved.
            raise EditorError(
                f"editor exited with status {completed.returncode}; the draft was left unchanged"
            )

        edited = path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)

    new_headline, new_body = parse_edited(edited)
    changed = (new_headline, new_body) != (headline.strip(), body.strip())
    return EditResult(headline=new_headline, body=new_body, changed=changed)
