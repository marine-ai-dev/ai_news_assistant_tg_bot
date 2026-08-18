"""Static assertions against .github/workflows/ai-news-publish.yml. Never skip these.

The pipeline-level tests in test_automation.py prove what ``run_automation()`` does for
a given ``mode``. Nothing there can prove the workflow actually *asks* for the right
mode at the right time, or only commits state under the right conditions — that lives
entirely in YAML this project's own test suite otherwise never reads. These tests read
it, the same way the rest of the safety suite reads source files rather than trusting a
docstring's claim about them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.safety

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ai-news-publish.yml"
)


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text: str) -> dict[str, Any]:
    return yaml.safe_load(workflow_text)


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return workflow["jobs"]["automate"]["steps"]


def _step(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    for step in _steps(workflow):
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


class TestPermissionsAndConcurrency:
    def test_permissions_are_contents_write_only(self, workflow: dict[str, Any]) -> None:
        assert workflow["permissions"] == {"contents": "write"}

    def test_concurrency_group_never_cancels_an_in_flight_run(
        self, workflow: dict[str, Any]
    ) -> None:
        concurrency = workflow["concurrency"]
        assert concurrency["group"] == "ai-news-publish"
        assert concurrency["cancel-in-progress"] is False


class TestModeSelection:
    def test_workflow_dispatch_defaults_to_dry_run(self, workflow: dict[str, Any]) -> None:
        mode_input = workflow[True]["workflow_dispatch"]["inputs"]["mode"]
        assert mode_input["default"] == "dry-run"
        assert set(mode_input["options"]) == {"dry-run", "test", "live"}

    def test_schedule_runs_monday_through_friday(self, workflow: dict[str, Any]) -> None:
        crons = [entry["cron"] for entry in workflow[True]["schedule"]]
        assert len(crons) == 1
        _minute, _hour, _dom, _month, dow = crons[0].split()
        assert dow == "1-5"

    def test_resolve_mode_step_never_reads_inputs_outside_the_dispatch_guard(
        self, workflow: dict[str, Any]
    ) -> None:
        """A schedule trigger has no `inputs` context at all. This step must only ever
        reference `inputs.mode` inside a branch already gated on
        `github.event_name == 'workflow_dispatch'` — never unconditionally, where a
        scheduled run could pick up a stray/empty value instead of the hard-coded
        "live" its `else` branch assigns.
        """
        script = _step(workflow, "Resolve mode")["run"]
        lines = script.splitlines()

        guard_line = next(
            i for i, line in enumerate(lines) if "github.event_name" in line and "if" in line
        )
        assert "workflow_dispatch" in lines[guard_line]

        else_line = next(i for i, line in enumerate(lines) if line.strip() == "else")
        fi_line = next(i for i, line in enumerate(lines) if line.strip() == "fi")
        assert guard_line < else_line < fi_line

        for i, line in enumerate(lines):
            if "inputs.mode" not in line:
                continue
            assert guard_line < i < else_line, (
                f"line {i} reads inputs.mode outside the workflow_dispatch guard: "
                f"{line!r}"
            )

        # The scheduled branch hard-codes live — not a fallback expression that could
        # silently evaluate to something else.
        else_body = lines[else_line + 1]
        assert 'mode="live"' in else_body

    def test_schedule_always_resolves_to_live_never_reading_inputs_at_all(
        self, workflow: dict[str, Any]
    ) -> None:
        """Belt-and-suspenders on the test above, phrased the other way round: the
        literal string 'inputs.mode' must not appear anywhere outside the
        if/else this step's own script uses to guard it."""
        script = _step(workflow, "Resolve mode")["run"]
        occurrences = [
            i for i, line in enumerate(script.splitlines()) if "inputs.mode" in line
        ]
        assert len(occurrences) == 1, (
            "expected exactly one guarded reference to inputs.mode; "
            f"found {len(occurrences)}"
        )


class TestKillSwitchWiring:
    def test_automation_enabled_is_passed_through_not_hard_coded(
        self, workflow_text: str
    ) -> None:
        """The workflow itself must never decide the kill switch's value — that
        decision belongs entirely to run_automation() (live-only) — it only has to
        forward whatever the repository Variable says."""
        assert "vars.AI_NEWS_AUTOMATION_ENABLED" in workflow_text

    def test_the_workflow_never_special_cases_dry_run_or_test_around_the_switch(
        self, workflow_text: str
    ) -> None:
        """No shell conditional in this file should gate dry-run or test behind the
        kill switch — that gate exists exactly once, inside the application."""
        assert not re.search(
            r"if.*AUTOMATION_ENABLED.*(dry-run|test)", workflow_text, re.IGNORECASE
        )


class TestRepositoryVariableWiring:
    def test_daily_post_limit_is_forwarded_from_the_repository_variable(
        self, workflow: dict[str, Any]
    ) -> None:
        """Settings.daily_post_limit has a real default (3) and no code path reads it
        from anywhere but the environment — a repository Variable that exists and is
        never forwarded here would silently have no effect at all."""
        env = _step(workflow, "Run automation")["env"]
        assert env["AI_NEWS_DAILY_POST_LIMIT"] == "${{ vars.AI_NEWS_DAILY_POST_LIMIT }}"

    def test_llm_model_is_forwarded_from_the_repository_variable(
        self, workflow: dict[str, Any]
    ) -> None:
        env = _step(workflow, "Run automation")["env"]
        assert env["AI_NEWS_LLM_MODEL"] == "${{ vars.AI_NEWS_LLM_MODEL }}"

    def test_gemini_read_timeout_is_forwarded_from_the_repository_variable(
        self, workflow: dict[str, Any]
    ) -> None:
        """A blank/absent Variable must fall back to Settings' own default (90s), not
        error — see Settings._blank_means_the_default, exercised directly in
        tests/unit/test_settings.py::TestGeminiReadTimeout."""
        env = _step(workflow, "Run automation")["env"]
        assert (
            env["AI_NEWS_GEMINI_READ_TIMEOUT_SECONDS"]
            == "${{ vars.AI_NEWS_GEMINI_READ_TIMEOUT_SECONDS }}"
        )


class TestStatePersistence:
    def test_persist_step_requires_live_mode_and_a_published_outcome(
        self, workflow: dict[str, Any]
    ) -> None:
        condition = _step(workflow, "Persist automation state")["if"]
        assert "steps.mode.outputs.mode == 'live'" in condition
        assert "steps.run.outputs.outcome == 'PUBLISHED'" in condition
        assert "&&" in condition

    def test_persist_step_condition_does_not_merely_exclude_dry_run(
        self, workflow: dict[str, Any]
    ) -> None:
        """Guards against regressing to the old, looser 'anything but dry-run' gate,
        which would also commit after test mode and after a live run that failed or
        published nothing."""
        condition = _step(workflow, "Persist automation state")["if"]
        assert "!= 'dry-run'" not in condition
        assert "always()" not in condition

    def test_git_add_is_targeted_never_a_bulk_add(self, workflow_text: str) -> None:
        # Bash comment lines are excluded — this file's own comments explain, in
        # prose, exactly why `git add -A` is never used, which would otherwise trip
        # a naive whole-file string search on its own explanation.
        code_lines = "\n".join(
            line for line in workflow_text.splitlines() if not line.strip().startswith("#")
        )
        assert "git add -f" in code_lines
        assert re.search(r"git add\s+-A\b", code_lines) is None
        assert re.search(r"git add\s+\.\s*$", code_lines, re.MULTILINE) is None

    def test_the_committed_path_is_outside_the_gitignored_dev_data_dir(
        self, workflow_text: str
    ) -> None:
        """AI_NEWS_DATA_DIR must not be the plain 'data' the local .env.example ships
        (that directory, and every *.sqlite3 file, is git-ignored everywhere) — a
        force-add still works either way, but a distinct path makes the intent legible
        rather than fighting .gitignore silently on every run."""
        match = re.search(r"AI_NEWS_DATA_DIR:\s*(\S+)", workflow_text)
        assert match is not None
        assert match.group(1) != "data"

    def test_no_secret_is_ever_echoed(self, workflow_text: str) -> None:
        for line in workflow_text.splitlines():
            if "secrets." not in line:
                continue
            # The only legitimate shape is assigning a secret to an env var for a
            # step to consume — never printing, echoing, or interpolating one into a
            # log line, a commit message, or a git remote URL.
            assert re.match(r"\s*AI_NEWS_\w+:\s*\$\{\{\s*secrets\.\w+\s*\}\}\s*$", line), (
                f"a secret is referenced somewhere other than a plain env assignment: "
                f"{line!r}"
            )

    def test_push_retries_are_bounded(self, workflow_text: str) -> None:
        assert re.search(r"attempt.*-ge\s*3", workflow_text) is not None

    def test_the_git_identity_is_a_bot_not_a_person(self, workflow_text: str) -> None:
        assert 'user.name "github-actions[bot]"' in workflow_text
        assert "users.noreply.github.com" in workflow_text
