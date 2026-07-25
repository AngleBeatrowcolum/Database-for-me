from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.agent.task_tools import (
    SQLiteOneTimeReminderAdapter,
    create_compatibility_task_tools,
    create_task_tools,
)
from app.agent.tools import ToolRegistry
from app.tasks.models import DeliveryStatus, ReminderOccurrenceStatus
from app.tasks.repository import ReminderRepository, TaskRepository
from app.tasks.service import TaskService


def _services(task_database, fixed_now) -> tuple[TaskService, SQLiteOneTimeReminderAdapter]:
    reminders = ReminderRepository(task_database)
    task_service = TaskService(
        task_database,
        TaskRepository(task_database),
        reminders,
        clock=lambda: fixed_now,
    )
    return task_service, SQLiteOneTimeReminderAdapter(reminders, clock=lambda: fixed_now)


def _registry(task_database, fixed_now) -> ToolRegistry:
    task_service, reminder_scheduler = _services(task_database, fixed_now)
    return ToolRegistry(create_task_tools(task_service, reminder_scheduler))


def test_task_tool_factory_registers_task_lifecycle_tools(task_database, fixed_now) -> None:
    registry = _registry(task_database, fixed_now)

    assert {tool.name for tool in registry.all()} == {
        "task_create",
        "task_update",
        "task_complete",
        "task_cancel",
        "task_reopen",
        "task_query",
    }


def test_task_create_complete_and_pending_query_use_task_service(task_database, fixed_now) -> None:
    registry = _registry(task_database, fixed_now)

    created = registry.execute(
        "task_create",
        {
            "title": "整理 SQLite 工具",
            "priority": "high",
            "planned_date": "2026-07-26",
            "due_at": (fixed_now + timedelta(days=1)).isoformat(),
        },
    )

    assert created.success
    assert created.content["task"]["title"] == "整理 SQLite 工具"
    assert created.content["task"]["priority"] == "high"
    assert created.content["task"]["planned_date"] == "2026-07-26"
    assert "标题：整理 SQLite 工具" in created.content["message"]
    assert "优先级：高" in created.content["message"]
    assert "计划日期：2026-07-26" in created.content["message"]
    assert "截止时间：" in created.content["message"]
    assert "提醒状态：" in created.content["message"]

    listed_before_completion = registry.execute("task_query", {"scope": "pending"})
    assert listed_before_completion.success
    assert "标题：整理 SQLite 工具" in listed_before_completion.content["message"]

    completed = registry.execute("task_complete", {"task_ref": created.content["task"]["id"]})
    assert completed.success
    assert completed.content["task"]["status"] == "completed"

    pending = registry.execute("task_query", {"scope": "pending"})
    assert pending.success
    assert pending.content["tasks"] == []


def test_task_tools_reject_unknown_scopes_and_invalid_repeated_completion(task_database, fixed_now) -> None:
    registry = _registry(task_database, fixed_now)
    created = registry.execute("task_create", {"title": "只完成一次"})
    assert created.success

    invalid_scope = registry.execute("task_query", {"scope": "all"})
    assert not invalid_scope.success
    assert "枚举" in invalid_scope.error

    assert registry.execute("task_complete", {"task_ref": created.content["task"]["id"]}).success
    repeated = registry.execute("task_complete", {"task_ref": created.content["task"]["id"]})
    assert not repeated.success
    assert "状态" in repeated.error


def test_task_create_can_explicitly_allow_a_past_due_date_without_deadline_reminders(
    task_database, fixed_now
) -> None:
    registry = _registry(task_database, fixed_now)
    past_due = (fixed_now - timedelta(minutes=1)).isoformat()

    rejected = registry.execute("task_create", {"title": "过期任务", "due_at": past_due})
    assert not rejected.success
    assert "确认" in rejected.error

    created = registry.execute(
        "task_create",
        {"title": "已确认的过期任务", "due_at": past_due, "allow_past_due": True},
    )

    assert created.success
    with task_database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM reminder_rules WHERE task_id = ?",
            (created.content["task"]["id"],),
        ).fetchone()[0]
    assert count == 0


def test_sqlite_reminder_adapter_creates_lists_and_cancels_one_time_records(
    task_database, fixed_now
) -> None:
    task_service, reminder_scheduler = _services(task_database, fixed_now)
    registry = ToolRegistry(
        [
            *create_task_tools(task_service, reminder_scheduler),
            *create_compatibility_task_tools(task_service, reminder_scheduler),
        ]
    )

    added = registry.execute(
        "add_reminder",
        {"text": "喝水", "delay_minutes": 5, "repeat": None},
    )

    assert added.success
    reminder_id = added.content["reminder"]["id"]
    assert added.content["reminder"]["repeat"] is None
    assert added.content["reminder"]["trigger_at"].startswith("2026-07-25T04:05:00")
    with task_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reminder_rules").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM reminder_occurrences").fetchone()[0] == 1

    listed = registry.execute("list_reminders", {})
    assert listed.success
    assert [reminder["id"] for reminder in listed.content["reminders"]] == [reminder_id]

    cancelled = registry.execute("cancel_reminder", {"id": reminder_id})
    assert cancelled.success
    assert cancelled.content["reminder"]["cancelled_at"] is not None
    with task_database.connect() as connection:
        occurrence = connection.execute(
            "SELECT status FROM reminder_occurrences WHERE id = ?", (reminder_id,)
        ).fetchone()
        delivery = connection.execute(
            "SELECT status FROM notification_deliveries"
        ).fetchone()
    assert occurrence["status"] == ReminderOccurrenceStatus.CANCELLED.value
    assert delivery["status"] != DeliveryStatus.SENDING.value
    assert registry.execute("list_reminders", {}).content["reminders"] == []


def test_compatibility_tools_write_sqlite_and_never_create_legacy_json(
    task_database, fixed_now, tmp_path: Path
) -> None:
    from app.agent.builtin_tools import create_builtin_tool_registry

    registry = create_builtin_tool_registry(
        tmp_path,
        task_service=_services(task_database, fixed_now)[0],
        reminder_scheduler=_services(task_database, fixed_now)[1],
    )

    added = registry.execute("add_todo", {"text": "旧工具也进 SQLite"})
    assert added.success
    task_id = added.content["task"]["id"]
    listed = registry.execute("list_todos", {})
    assert [task["id"] for task in listed.content["tasks"]] == [task_id]
    assert registry.execute("complete_todo", {"id": task_id}).success
    assert not (tmp_path / "data" / "tasks.json").exists()
    assert not (tmp_path / "data" / "reminders.json").exists()


def test_builtin_registry_has_explicit_error_path_without_task_service(tmp_path: Path) -> None:
    from app.agent.builtin_tools import create_builtin_tool_registry

    registry = create_builtin_tool_registry(tmp_path)

    expected = {
        "task_create",
        "task_update",
        "task_complete",
        "task_cancel",
        "task_reopen",
        "task_query",
        "add_todo",
        "list_todos",
        "complete_todo",
        "add_reminder",
        "list_reminders",
        "cancel_reminder",
    }
    assert expected <= {tool.name for tool in registry.all()}
    unavailable = registry.execute("add_todo", {"text": "不能落到 JSON"})
    assert not unavailable.success
    assert "任务服务未初始化" in unavailable.error


def test_deprecated_reminder_store_is_a_sqlite_facade(task_database, fixed_now) -> None:
    from app.agent import ReminderStore

    _, reminder_scheduler = _services(task_database, fixed_now)
    with pytest.deprecated_call(match="ReminderStore"):
        store = ReminderStore(reminder_scheduler)

    added = store.add_reminder({"text": "门面提醒", "delay_seconds": 60})

    assert added["reminder"]["text"] == "门面提醒"
    with task_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reminder_rules").fetchone()[0] == 1


def test_deprecated_reminder_store_keeps_legacy_ui_polling_safe(task_database, fixed_now) -> None:
    from app.agent import ReminderStore

    _, reminder_scheduler = _services(task_database, fixed_now)
    with pytest.deprecated_call(match="ReminderStore"):
        store = ReminderStore(reminder_scheduler)

    assert store.due_reminders() == []
    assert store.mark_completed("old-ui-reminder") == {
        "id": "old-ui-reminder",
        "status": "not_scheduled",
    }
