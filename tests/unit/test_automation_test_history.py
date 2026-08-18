"""automation.test_history — the small, bounded, append-only file behind --test dedup."""

from __future__ import annotations

from pathlib import Path

from ai_news_editor.automation.test_history import MAX_ENTRIES, already_test_published, load, record


class TestLoad:
    def test_a_missing_file_is_empty_history(self, tmp_path: Path) -> None:
        assert load(tmp_path / "nope.json") == []

    def test_a_corrupt_file_is_treated_as_empty_not_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        path.write_text("{not json", encoding="utf-8")
        assert load(path) == []

    def test_a_file_with_the_wrong_shape_is_treated_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        path.write_text('["just", "a", "list"]', encoding="utf-8")
        assert load(path) == []

    def test_an_entry_missing_required_fields_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        path.write_text('{"entries": [{"source_url": "https://x.invalid"}]}', encoding="utf-8")
        assert load(path) == []


class TestRecordAndLoad:
    def test_a_recorded_entry_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        record(path, source_url="https://x.invalid/a", message_id=42)
        entries = load(path)
        assert len(entries) == 1
        assert entries[0].source_url == "https://x.invalid/a"
        assert entries[0].telegram_message_id == 42
        assert entries[0].published_at  # non-empty timestamp

    def test_recording_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "history.json"
        record(path, source_url="https://x.invalid/a", message_id=1)
        assert path.exists()

    def test_already_test_published_reflects_recorded_urls(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        assert already_test_published(path, "https://x.invalid/a") is False
        record(path, source_url="https://x.invalid/a", message_id=1)
        assert already_test_published(path, "https://x.invalid/a") is True
        assert already_test_published(path, "https://x.invalid/b") is False

    def test_entries_accumulate_across_multiple_records(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        record(path, source_url="https://x.invalid/a", message_id=1)
        record(path, source_url="https://x.invalid/b", message_id=2)
        urls = {entry.source_url for entry in load(path)}
        assert urls == {"https://x.invalid/a", "https://x.invalid/b"}


class TestBoundedRetention:
    def test_history_never_exceeds_max_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        for i in range(MAX_ENTRIES + 10):
            record(path, source_url=f"https://x.invalid/{i}", message_id=i)
        assert len(load(path)) == MAX_ENTRIES

    def test_the_oldest_entries_are_dropped_first(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        for i in range(MAX_ENTRIES + 5):
            record(path, source_url=f"https://x.invalid/{i}", message_id=i)
        urls = {entry.source_url for entry in load(path)}
        assert "https://x.invalid/0" not in urls
        assert "https://x.invalid/4" not in urls
        assert f"https://x.invalid/{MAX_ENTRIES + 4}" in urls
