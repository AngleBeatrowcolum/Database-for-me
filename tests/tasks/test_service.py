from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.tasks.errors import ConfirmationRequired
from app.tasks.models import DeliveryChannel, DeliveryStatus, Priority, TaskStatus, to_utc_text
from app.tasks.repository import ReminderRepository, TaskRepository
from app.tasks.service import (
    AmbiguousTaskReferenceError,
    InvalidTaskTransitionError,
    TaskNotFoundError,
    TaskService,
)


@pytest.fixture
def service(task_database, fixed_now) -> TaskService:
    return TaskService(
        task_database,
        TaskRepository(task_database),
        ReminderRepository(task_database),
        clock=lambda: fixed_now,
    )


def _task_rows(task_database, task_id: str) -> dict[str, int]:
    with task_database.connect() as connection:
        return {
            "tasks": connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0],
            "events": connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()[0],
            "rules": connection.execute(
                "SELECT COUNT(*) FROM reminder_rules WHERE task_id = ?", (task_id,)
            ).fetchone()[0],
            "occurrences": connection.execute(
                "SELECT COUNT(*) FROM reminder_occurrences WHERE task_id = ?", (task_id,)
            ).fetchone()[0],
            "deliveries": connection.execute(
                """
                SELECT COUNT(*)
                FROM notification_deliveries AS delivery
                JOIN reminder_occurrences AS occurrence ON occurrence.id = delivery.occurrence_id
                WHERE occurrence.task_id = ?
                """,
                (task_id,),
            ).fetchone()[0],
        }


def _archive_task(task_database, task, fixed_now) -> None:
    with task_database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO weekly_summary_runs
                (id, iso_year, iso_week, week_start, week_end, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("summary-1", 2026, 30, "2026-07-20", "2026-07-26", "published", to_utc_text(fixed_now)),
        )
        connection.execute(
            """
            INSERT INTO task_summary_archives
                (task_id, summary_run_id, task_updated_at, archived_at)
            VALUES (?, ?, ?, ?)
            """,
            (task.id, "summary-1", to_utc_text(task.updated_at), to_utc_text(fixed_now)),
        )


def test_create_task_creates_default_deadline_rules_occurrences_and_deliveries(
    service, task_database, fixed_now
) -> None:
    task = service.create_task(
        "完成报告",
        details="最终版",
        priority="high",
        planned_date="2026-07-26",
        due_at="2026-07-27T04:00:00Z",
        now=fixed_now,
    )

    assert task.status is TaskStatus.PENDING
    assert task.priority is Priority.HIGH
    assert task.planned_date == date(2026, 7, 26)
    assert task.due_at == fixed_now + timedelta(days=2)
    assert _task_rows(task_database, task.id) == {
        "tasks": 1,
        "events": 1,
        "rules": 2,
        "occurrences": 2,
        "deliveries": 4,
    }
    with task_database.connect() as connection:
        offsets = [
            row["offset_seconds"]
            for row in connection.execute(
                "SELECT offset_seconds FROM reminder_rules WHERE task_id = ? ORDER BY offset_seconds",
                (task.id,),
            )
        ]
        scheduled = [
            row["scheduled_at"]
            for row in connection.execute(
                "SELECT scheduled_at FROM reminder_occurrences WHERE task_id = ? ORDER BY scheduled_at",
                (task.id,),
            )
        ]
    assert offsets == [-86400, -7200]
    assert scheduled == ["2026-07-26T04:00:00.000000Z", "2026-07-27T02:00:00.000000Z"]


def test_create_task_with_date_only_does_not_invent_due_time(service, task_database, fixed_now) -> None:
    task = service.create_task("只计划", planned_date=date(2026, 7, 25), now=fixed_now)

    assert task.due_at is None
    assert _task_rows(task_database, task.id) == {
        "tasks": 1,
        "events": 1,
        "rules": 0,
        "occurrences": 0,
        "deliveries": 0,
    }


def test_create_past_due_requires_confirmation_or_saves_without_deadline_reminders(
    service, task_database, fixed_now
) -> None:
    with pytest.raises(ConfirmationRequired, match="截止时间已过去"):
        service.create_task("过去", due_at=fixed_now, now=fixed_now)

    saved = service.create_task(
        "确认过去", due_at=fixed_now - timedelta(seconds=1), allow_past_due=True, now=fixed_now
    )
    assert saved.due_at == fixed_now - timedelta(seconds=1)
    assert _task_rows(task_database, saved.id)["rules"] == 0


def test_update_rebuilds_future_deadline_reminders_and_clears_archive(
    service, task_database, fixed_now
) -> None:
    task = service.create_task("待更新", due_at=fixed_now + timedelta(days=2), now=fixed_now)
    _archive_task(task_database, task, fixed_now)

    updated = service.update_task(
        task.id,
        title="已更新",
        details="新的详情",
        priority=Priority.HIGH,
        due_at=fixed_now + timedelta(days=3),
        now=fixed_now + timedelta(minutes=1),
    )

    assert updated.title == "已更新"
    assert updated.priority is Priority.HIGH
    assert _task_rows(task_database, task.id) == {
        "tasks": 1,
        "events": 2,
        "rules": 4,
        "occurrences": 4,
        "deliveries": 8,
    }
    with task_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM task_summary_archives WHERE task_id = ?", (task.id,)
        ).fetchone() is None
        old_occurrences = connection.execute(
            """
            SELECT occurrence.status AS occurrence_status, delivery.status AS delivery_status
            FROM reminder_occurrences AS occurrence
            JOIN notification_deliveries AS delivery ON delivery.occurrence_id = occurrence.id
            WHERE occurrence.task_id = ? AND occurrence.scheduled_at < ?
            """,
            (task.id, to_utc_text(updated.due_at - timedelta(days=1))),
        ).fetchall()
        event = connection.execute(
            "SELECT before_json, after_json FROM task_events WHERE task_id = ? AND event_type = 'updated'",
            (task.id,),
        ).fetchone()
        rules = [
            dict(row)
            for row in connection.execute(
                """
                SELECT message, enabled FROM reminder_rules
                WHERE task_id = ? ORDER BY created_at, id
                """,
                (task.id,),
            )
        ]
    assert {
        (row["occurrence_status"], row["delivery_status"])
        for row in old_occurrences
    } == {("cancelled", "skipped")}
    assert rules == [
        {"message": "待更新", "enabled": 0},
        {"message": "待更新", "enabled": 0},
        {"message": "已更新", "enabled": 1},
        {"message": "已更新", "enabled": 1},
    ]
    # The event preserves meaningful before/after snapshots instead of only a status marker.
    assert json.loads(event["before_json"])["title"] == "待更新"
    assert json.loads(event["after_json"])["title"] == "已更新"


def test_complete_cancel_and_reopen_coordinate_status_and_pending_reminders(
    service, task_database, fixed_now
) -> None:
    task = service.create_task("生命周期", due_at=fixed_now + timedelta(days=2), now=fixed_now)
    completed = service.complete_task(task.id, now=fixed_now + timedelta(minutes=1))
    assert completed.status is TaskStatus.COMPLETED
    assert completed.completed_at == fixed_now + timedelta(minutes=1)

    with task_database.connect() as connection:
        assert set(
            row["status"]
            for row in connection.execute(
                "SELECT status FROM reminder_occurrences WHERE task_id = ?", (task.id,)
            )
        ) == {"cancelled"}
        assert set(
            row["status"]
            for row in connection.execute(
                """
                SELECT delivery.status
                FROM notification_deliveries AS delivery
                JOIN reminder_occurrences AS occurrence ON occurrence.id = delivery.occurrence_id
                WHERE occurrence.task_id = ?
                """,
                (task.id,),
            )
        ) == {DeliveryStatus.SKIPPED.value}
        assert {
            row["enabled"]
            for row in connection.execute(
                "SELECT enabled FROM reminder_rules WHERE task_id = ?", (task.id,)
            )
        } == {0}

    reopened = service.reopen_task(task.id, now=fixed_now + timedelta(minutes=2))
    assert reopened.status is TaskStatus.PENDING
    assert reopened.completed_at is None
    assert _task_rows(task_database, task.id)["rules"] == 4
    with task_database.connect() as connection:
        assert [
            row["enabled"]
            for row in connection.execute(
                "SELECT enabled FROM reminder_rules WHERE task_id = ? ORDER BY created_at, id",
                (task.id,),
            )
        ] == [0, 0, 1, 1]

    cancelled = service.cancel_task(task.id, now=fixed_now + timedelta(minutes=3))
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.cancelled_at == fixed_now + timedelta(minutes=3)
    with task_database.connect() as connection:
        assert {
            row["enabled"]
            for row in connection.execute(
                "SELECT enabled FROM reminder_rules WHERE task_id = ?", (task.id,)
            )
        } == {0}


def test_replacing_deadline_rules_preserves_sent_and_cancelled_history(
    service, task_database, fixed_now
) -> None:
    task = service.create_task("保留历史", due_at=fixed_now + timedelta(days=2), now=fixed_now)
    delivery = service.reminders.claim_due_deliveries(
        DeliveryChannel.EMAIL, fixed_now + timedelta(days=1), 1
    )[0]
    assert service.reminders.mark_delivery_sent(
        delivery.id, delivery.claim_token, fixed_now + timedelta(days=1)
    ) is True

    service.update_task(
        task.id,
        due_at=fixed_now + timedelta(days=3),
        now=fixed_now + timedelta(days=1, minutes=1),
    )

    with task_database.connect() as connection:
        sent = connection.execute(
            "SELECT status FROM notification_deliveries WHERE id = ?", (delivery.id,)
        ).fetchone()
        old_rules = connection.execute(
            """
            SELECT enabled FROM reminder_rules
            WHERE task_id = ? AND created_at = ?
            ORDER BY id
            """,
            (task.id, to_utc_text(fixed_now)),
        ).fetchall()
        old_occurrences = connection.execute(
            """
            SELECT status FROM reminder_occurrences
            WHERE task_id = ? AND created_at = ?
            """,
            (task.id, to_utc_text(fixed_now)),
        ).fetchall()
    assert sent["status"] == DeliveryStatus.SENT.value
    assert [row["enabled"] for row in old_rules] == [0, 0]
    assert {row["status"] for row in old_occurrences} == {"cancelled"}


def test_title_update_refreshes_only_enabled_deadline_rule_messages(
    service, task_database, fixed_now
) -> None:
    task = service.create_task("旧标题", due_at=fixed_now + timedelta(days=2), now=fixed_now)
    delivery = service.reminders.claim_due_deliveries(
        DeliveryChannel.EMAIL, fixed_now + timedelta(days=1), 1
    )[0]
    assert service.reminders.mark_delivery_sent(
        delivery.id, delivery.claim_token, fixed_now + timedelta(days=1)
    ) is True
    service.update_task(
        task.id,
        due_at=fixed_now + timedelta(days=3),
        now=fixed_now + timedelta(days=1, minutes=1),
    )

    service.update_task(
        task.id,
        title="新标题",
        now=fixed_now + timedelta(days=1, minutes=2),
    )

    with task_database.connect() as connection:
        rules = [
            dict(row)
            for row in connection.execute(
                """
                SELECT message, enabled FROM reminder_rules
                WHERE task_id = ? ORDER BY created_at, id
                """,
                (task.id,),
            )
        ]
        sent = connection.execute(
            "SELECT status FROM notification_deliveries WHERE id = ?", (delivery.id,)
        ).fetchone()
    assert rules == [
        {"message": "旧标题", "enabled": 0},
        {"message": "旧标题", "enabled": 0},
        {"message": "新标题", "enabled": 1},
        {"message": "新标题", "enabled": 1},
    ]
    assert sent["status"] == DeliveryStatus.SENT.value


def test_rejects_invalid_state_transitions_with_domain_error(service, fixed_now) -> None:
    task = service.create_task("状态", now=fixed_now)
    service.complete_task(task.id, now=fixed_now + timedelta(seconds=1))

    with pytest.raises(InvalidTaskTransitionError, match="不允许"):
        service.complete_task(task.id, now=fixed_now + timedelta(seconds=2))
    with pytest.raises(InvalidTaskTransitionError, match="只能通过"):
        service.update_task(task.id, status="pending", now=fixed_now + timedelta(seconds=3))


def test_resolve_task_requires_unique_exact_title_and_reports_missing(service, fixed_now) -> None:
    only = service.create_task("唯一标题", now=fixed_now)
    assert service.resolve_task("唯一标题") == only
    assert service.resolve_task(only.id) == only

    first_duplicate = service.create_task(
        "重复标题", due_at=fixed_now + timedelta(days=1), now=fixed_now
    )
    second_duplicate = service.create_task(
        "重复标题",
        due_at=fixed_now + timedelta(days=2),
        now=fixed_now + timedelta(seconds=1),
    )
    with pytest.raises(AmbiguousTaskReferenceError, match="不唯一") as read_error:
        service.resolve_task("重复标题")
    assert isinstance(read_error.value.candidates, tuple)
    assert set(read_error.value.candidates) == {first_duplicate, second_duplicate}
    assert {
        (task.id, task.title, task.due_at)
        for task in read_error.value.candidates
    } == {
        (first_duplicate.id, "重复标题", fixed_now + timedelta(days=1)),
        (second_duplicate.id, "重复标题", fixed_now + timedelta(days=2)),
    }
    with pytest.raises(TaskNotFoundError, match="任务不存在"):
        service.resolve_task("不存在")
    with pytest.raises(AmbiguousTaskReferenceError) as write_error:
        service.complete_task("重复标题", now=fixed_now + timedelta(seconds=2))
    assert write_error.value.candidates == read_error.value.candidates


def test_create_rolls_back_task_events_rules_occurrences_and_deliveries_on_failure(
    task_database, fixed_now
) -> None:
    class FailingReminderRepository(ReminderRepository):
        def create_deadline_rule(self, *args, **kwargs):
            rule = super().create_deadline_rule(*args, **kwargs)
            if rule.offset_seconds == -7200:
                raise RuntimeError("injected reminder failure")
            return rule

    service = TaskService(
        task_database,
        TaskRepository(task_database),
        FailingReminderRepository(task_database),
        clock=lambda: fixed_now,
    )
    with pytest.raises(RuntimeError, match="injected reminder failure"):
        service.create_task("回滚", due_at=fixed_now + timedelta(days=1), now=fixed_now)

    with task_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM reminder_rules").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM reminder_occurrences").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0


def test_constructor_accepts_an_injectable_clock_as_its_fourth_argument(
    task_database, fixed_now
) -> None:
    service = TaskService(
        task_database,
        TaskRepository(task_database),
        ReminderRepository(task_database),
        lambda: fixed_now,
    )

    assert service.create_task("使用时钟").created_at == fixed_now


def test_closed_task_due_update_does_not_recreate_active_deadline_reminders(
    service, task_database, fixed_now
) -> None:
    task = service.create_task("已关闭", due_at=fixed_now + timedelta(days=2), now=fixed_now)
    service.complete_task(task.id, now=fixed_now + timedelta(minutes=1))

    service.update_task(
        task.id,
        due_at=fixed_now + timedelta(days=3),
        now=fixed_now + timedelta(minutes=2),
    )

    assert _task_rows(task_database, task.id)["rules"] == 2
    with task_database.connect() as connection:
        assert set(
            row["status"]
            for row in connection.execute(
                "SELECT status FROM reminder_occurrences WHERE task_id = ?", (task.id,)
            )
        ) == {"cancelled"}


def test_future_due_keeps_two_default_rules_but_omits_elapsed_occurrences(
    service, task_database, fixed_now
) -> None:
    task = service.create_task("临近截止", due_at=fixed_now + timedelta(hours=3), now=fixed_now)

    assert _task_rows(task_database, task.id) == {
        "tasks": 1,
        "events": 1,
        "rules": 2,
        "occurrences": 1,
        "deliveries": 2,
    }
    with task_database.connect() as connection:
        scheduled_at = connection.execute(
            "SELECT scheduled_at FROM reminder_occurrences WHERE task_id = ?", (task.id,)
        ).fetchone()["scheduled_at"]
    assert scheduled_at == to_utc_text(fixed_now + timedelta(hours=1))


def test_write_operation_resolves_its_task_after_entering_immediate_transaction(
    service, fixed_now, monkeypatch
) -> None:
    task = service.create_task("事务内解析", now=fixed_now)

    def fail_if_called(reference: str):
        raise AssertionError(f"write operation resolved stale reference: {reference}")

    monkeypatch.setattr(service, "resolve_task", fail_if_called)
    completed = service.complete_task(task.id, now=fixed_now + timedelta(seconds=1))

    assert completed.status is TaskStatus.COMPLETED


def test_query_today_delegates_to_today_query_service(service, fixed_now) -> None:
    service.create_task("今日接口", planned_date="2026-07-25", now=fixed_now)

    result = service.query_today(fixed_now)

    assert result.summary.total == 1
    assert [task.title for task in result.planned_today] == ["今日接口"]
