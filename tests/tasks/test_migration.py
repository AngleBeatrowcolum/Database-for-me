from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.tasks.migration as migration_module
from app.tasks.migration import LegacyJsonMigrator
from app.tasks.models import parse_utc


def _write_legacy(legacy_dir: Path, *, tasks: list[dict], reminders: list[dict]) -> None:
    legacy_dir.mkdir()
    (legacy_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8"
    )
    (legacy_dir / "reminders.json").write_text(
        json.dumps({"reminders": reminders}, ensure_ascii=False), encoding="utf-8"
    )


def test_migrates_real_legacy_json_once_and_keeps_immutable_source_backup(
    task_database, tmp_path: Path
) -> None:
    legacy_dir = tmp_path / "legacy"
    _write_legacy(
        legacy_dir,
        tasks=[
            {
                "id": "old-task",
                "text": "  完成迁移  ",
                "created_at": "2026-07-25T12:00:00+08:00",
                "completed_at": None,
            }
        ],
        reminders=[
            {
                "id": "old-reminder",
                "text": "喝水",
                "trigger_at": "2026-07-25T13:30:00+08:00",
                "repeat": None,
                "created_at": "2026-07-25T12:00:00+08:00",
                "completed_at": None,
                "cancelled_at": None,
            }
        ],
    )
    tasks_before = (legacy_dir / "tasks.json").read_bytes()
    reminders_before = (legacy_dir / "reminders.json").read_bytes()

    result = LegacyJsonMigrator(task_database, legacy_dir).run(
        now=datetime(2026, 7, 25, 4, tzinfo=timezone.utc)
    )

    assert result.tasks_imported == 1
    assert result.reminders_imported == 1
    assert len(result.backup_paths) == 2
    assert (legacy_dir / "tasks.json").read_bytes() == tasks_before
    assert (legacy_dir / "reminders.json").read_bytes() == reminders_before
    assert {path.name for path in result.backup_paths} == {"tasks.json", "reminders.json"}
    assert [path.read_bytes() for path in result.backup_paths if path.name == "tasks.json"] == [
        tasks_before
    ]

    with task_database.connect() as connection:
        task = connection.execute("SELECT * FROM tasks").fetchone()
        assert task["id"] != "old-task"
        assert task["title"] == "完成迁移"
        assert task["details"] == ""
        assert task["status"] == "pending"
        assert task["priority"] == "normal"
        assert task["planned_date"] is None
        assert task["due_at"] is None
        assert parse_utc(task["created_at"]) == datetime(2026, 7, 25, 4, tzinfo=timezone.utc)
        assert connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1
        rule = connection.execute("SELECT * FROM reminder_rules").fetchone()
        occurrence = connection.execute("SELECT * FROM reminder_occurrences").fetchone()
        assert rule["kind"] == "one_time"
        assert (rule["desktop_enabled"], rule["email_enabled"]) == (1, 0)
        assert parse_utc(occurrence["scheduled_at"]) == datetime(
            2026, 7, 25, 5, 30, tzinfo=timezone.utc
        )
        assert occurrence["status"] == "pending"
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 1
        assert connection.execute(
            "SELECT value FROM maintenance_state WHERE key = 'legacy_json_v1_completed'"
        ).fetchone()[0] == "true"

    repeated = LegacyJsonMigrator(task_database, legacy_dir).run()
    assert repeated.tasks_imported == 0
    assert repeated.reminders_imported == 0
    assert repeated.backup_paths == ()
    with task_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1


def test_migration_normalizes_timezones_and_preserves_terminal_states(
    task_database, tmp_path: Path
) -> None:
    legacy_dir = tmp_path / "legacy"
    _write_legacy(
        legacy_dir,
        tasks=[
            {
                "id": "completed-task",
                "title": "已完成",
                "created_at": "2026-07-25T00:00:00Z",
                "completed_at": "2026-07-25T12:00:00+08:00",
            },
            {
                "id": "cancelled-task",
                "text": "已取消",
                "created_at": "2026-07-25T00:00:00Z",
                "cancelled_at": "2026-07-25T01:00:00Z",
            },
        ],
        reminders=[
            {
                "id": "completed-reminder",
                "text": "已完成提醒",
                "trigger_at": "2026-07-25T12:00:00+08:00",
                "repeat": None,
                "created_at": "2026-07-25T00:00:00Z",
                "completed_at": "2026-07-25T12:01:00+08:00",
                "channels": ["desktop"],
            },
            {
                "id": "cancelled-reminder",
                "text": "已取消提醒",
                "trigger_at": "2026-07-25T01:00:00Z",
                "repeat": None,
                "created_at": "2026-07-25T00:00:00Z",
                "cancelled_at": "2026-07-25T01:01:00Z",
                "desktop_enabled": False,
                "email_enabled": True,
            },
        ],
    )

    result = LegacyJsonMigrator(task_database, legacy_dir).run()
    assert (result.tasks_imported, result.reminders_imported) == (2, 2)
    with task_database.connect() as connection:
        tasks = connection.execute(
            "SELECT title, status, completed_at, cancelled_at FROM tasks ORDER BY title"
        ).fetchall()
        assert [(row["title"], row["status"]) for row in tasks] == [
            ("已取消", "cancelled"),
            ("已完成", "completed"),
        ]
        completed = next(row for row in tasks if row["status"] == "completed")
        assert parse_utc(completed["completed_at"]) == datetime(
            2026, 7, 25, 4, tzinfo=timezone.utc
        )
        rows = connection.execute(
            """
            SELECT occurrence.status AS occurrence_status, delivery.status AS delivery_status,
                   delivery.channel
            FROM reminder_occurrences AS occurrence
            JOIN notification_deliveries AS delivery ON delivery.occurrence_id = occurrence.id
            ORDER BY delivery.channel, occurrence.id
            """
        ).fetchall()
        assert {row["occurrence_status"] for row in rows} == {"completed", "cancelled"}
        assert {row["delivery_status"] for row in rows} <= {"sent", "skipped"}
        assert {row["delivery_status"] for row in rows}.isdisjoint({"pending", "sending"})


def test_malformed_legacy_data_rolls_back_all_sqlite_writes_and_leaves_unmarked(
    task_database, tmp_path: Path
) -> None:
    legacy_dir = tmp_path / "legacy"
    _write_legacy(
        legacy_dir,
        tasks=[
            {
                "id": "would-import",
                "text": "有效任务",
                "created_at": "2026-07-25T00:00:00Z",
                "completed_at": None,
            }
        ],
        reminders=[
            {
                "id": "bad-reminder",
                "text": " ",
                "trigger_at": "2026-07-25T01:00:00Z",
                "repeat": None,
                "created_at": "2026-07-25T00:00:00Z",
            }
        ],
    )

    with pytest.raises(ValueError):
        LegacyJsonMigrator(task_database, legacy_dir).run()

    with task_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM reminder_rules").fetchone()[0] == 0
        assert connection.execute(
            "SELECT 1 FROM maintenance_state WHERE key = 'legacy_json_v1_completed'"
        ).fetchone() is None


def test_failed_source_backup_leaves_no_partial_backup_files(
    task_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_dir = tmp_path / "legacy"
    _write_legacy(legacy_dir, tasks=[], reminders=[])
    backup_root = tmp_path / "source-backups"

    def fail_after_creating_target(source: Path, target: Path) -> None:
        target.write_bytes(b"partial")
        raise OSError("forced copy failure")

    monkeypatch.setattr(
        migration_module, "_copy_without_overwrite", fail_after_creating_target
    )
    with pytest.raises(OSError, match="forced copy failure"):
        LegacyJsonMigrator(task_database, legacy_dir, backup_root=backup_root).run()

    assert not list(backup_root.rglob("*.json"))
    with task_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM maintenance_state WHERE key = 'legacy_json_v1_completed'"
        ).fetchone() is None
