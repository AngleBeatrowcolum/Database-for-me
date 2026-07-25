import json
from dataclasses import replace
from datetime import timedelta

import pytest

from app.tasks.models import (
    DeliveryChannel,
    DeliveryStatus,
    Priority,
    ReminderKind,
    ReminderOccurrenceStatus,
    Task,
    to_utc_text,
)
from app.tasks.repository import ReminderRepository, TaskRepository


def test_task_round_trip_and_unique_delivery_claim(task_database, fixed_now) -> None:
    tasks = TaskRepository(task_database)
    reminders = ReminderRepository(task_database)
    task = Task.new("完成实验报告", now=fixed_now, due_at=fixed_now + timedelta(days=1))
    tasks.insert(task, event_type="created")
    assert tasks.get(task.id) == task

    rule = reminders.create_deadline_rule(task, offset_seconds=-7200, now=fixed_now)
    occurrence = reminders.ensure_occurrence(
        rule, scheduled_at=task.due_at - timedelta(hours=2), now=fixed_now
    )
    first = reminders.claim_due_deliveries(
        DeliveryChannel.EMAIL, now=task.due_at, limit=10
    )
    second = reminders.claim_due_deliveries(
        DeliveryChannel.EMAIL, now=task.due_at, limit=10
    )
    assert [item.occurrence_id for item in first] == [occurrence.id]
    assert second == []


def test_task_update_audits_canonical_json_and_queries_in_order(
    task_database, fixed_now
) -> None:
    tasks = TaskRepository(task_database)
    first = Task.new(
        "相同标题", now=fixed_now, due_at=fixed_now + timedelta(days=3)
    )
    second = Task.new(
        "相同标题", now=fixed_now + timedelta(seconds=1), due_at=fixed_now + timedelta(days=1)
    )
    unscheduled = Task.new("未安排", now=fixed_now + timedelta(seconds=2))
    tasks.insert(first, event_type="created")
    tasks.insert(second, event_type="created")
    tasks.insert(unscheduled, event_type="created")

    with task_database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO weekly_summary_runs
                (id, iso_year, iso_week, week_start, week_end, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "summary-1",
                2026,
                30,
                "2026-07-20",
                "2026-07-26",
                "published",
                to_utc_text(fixed_now),
            ),
        )
        connection.execute(
            """
            INSERT INTO task_summary_archives
                (task_id, summary_run_id, task_updated_at, archived_at)
            VALUES (?, ?, ?, ?)
            """,
            (first.id, "summary-1", to_utc_text(first.updated_at), to_utc_text(fixed_now)),
        )

    updated = replace(
        first,
        title="中文已更新",
        details="详情",
        priority=Priority.HIGH,
        updated_at=fixed_now + timedelta(minutes=1),
    )
    tasks.update(updated, event_type="updated", before=first)
    with task_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM task_summary_archives WHERE task_id = ?", (updated.id,)
        ).fetchone() is None

    events = tasks.list_events_between(fixed_now, fixed_now + timedelta(hours=1))
    update_event = next(event for event in events if event["event_type"] == "updated")
    assert update_event["before_json"] == json.dumps(
        json.loads(update_event["before_json"]), ensure_ascii=False, sort_keys=True
    )
    assert update_event["after_json"] == json.dumps(
        json.loads(update_event["after_json"]), ensure_ascii=False, sort_keys=True
    )
    assert "中文已更新" in update_event["after_json"]
    assert json.loads(update_event["before_json"])["title"] == "相同标题"
    assert json.loads(update_event["after_json"])["priority"] == "high"

    assert tasks.find_pending_by_exact_title("相同标题") == [second]
    assert tasks.list_pending() == [second, updated, unscheduled]

    with task_database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_summary_archives
                (task_id, summary_run_id, task_updated_at, archived_at)
            VALUES (?, ?, ?, ?)
            """,
            (updated.id, "summary-1", to_utc_text(updated.updated_at), to_utc_text(fixed_now)),
        )
    tasks.clear_archive_marker(updated.id)
    with task_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM task_summary_archives WHERE task_id = ?", (updated.id,)
        ).fetchone() is None


def test_rules_create_expected_occurrences_and_channel_deliveries(
    task_database, fixed_now
) -> None:
    tasks = TaskRepository(task_database)
    reminders = ReminderRepository(task_database)
    task = Task.new("截止任务", now=fixed_now, due_at=fixed_now + timedelta(days=1))
    tasks.insert(task, event_type="created")

    deadline = reminders.create_deadline_rule(task, offset_seconds=-7200, now=fixed_now)
    assert deadline.task_id == task.id
    assert deadline.message == task.title
    assert deadline.kind is ReminderKind.DEADLINE_OFFSET
    assert deadline.offset_seconds == -7200
    assert deadline.desktop_enabled is True
    assert deadline.email_enabled is True

    scheduled_at = task.due_at - timedelta(hours=2)
    deadline_occurrence = reminders.ensure_occurrence(deadline, scheduled_at, fixed_now)
    assert reminders.ensure_occurrence(deadline, scheduled_at, fixed_now) == deadline_occurrence

    one_time_at = fixed_now + timedelta(hours=1)
    one_time = reminders.create_one_time_rule(
        "只弹桌面", one_time_at, {DeliveryChannel.DESKTOP}, fixed_now
    )
    assert one_time.task_id is None
    assert one_time.kind is ReminderKind.ONE_TIME
    assert one_time.offset_seconds is None
    assert one_time.weekdays_mask is None
    assert one_time.time_of_day is None
    assert one_time.desktop_enabled is True
    assert one_time.email_enabled is False

    weekly = reminders.create_weekly_rule(
        "每周提醒", weekdays_mask=0b0111110, time_of_day="14:00:00", grace_seconds=1800,
        now=fixed_now,
    )
    weekly_occurrence = reminders.ensure_occurrence(
        weekly, fixed_now + timedelta(days=2), fixed_now
    )
    assert weekly.kind is ReminderKind.WEEKLY
    assert weekly.weekdays_mask == 0b0111110
    assert weekly.time_of_day == "14:00:00"
    assert weekly.grace_seconds == 1800
    assert weekly_occurrence.expires_at == weekly_occurrence.scheduled_at + timedelta(minutes=30)

    with task_database.connect() as connection:
        deadline_channels = connection.execute(
            "SELECT channel FROM notification_deliveries WHERE occurrence_id = ? ORDER BY channel",
            (deadline_occurrence.id,),
        ).fetchall()
        one_time_channels = connection.execute(
            "SELECT channel FROM notification_deliveries "
            "WHERE occurrence_id = (SELECT id FROM reminder_occurrences WHERE rule_id = ?)",
            (one_time.id,),
        ).fetchall()
    assert [row["channel"] for row in deadline_channels] == ["desktop", "email"]
    assert [row["channel"] for row in one_time_channels] == ["desktop"]


def test_delivery_leases_recover_and_reject_wrong_token(task_database, fixed_now) -> None:
    reminders = ReminderRepository(task_database)
    first_rule = reminders.create_one_time_rule(
        "第一条", fixed_now, {DeliveryChannel.DESKTOP}, fixed_now
    )
    second_rule = reminders.create_one_time_rule(
        "第二条", fixed_now + timedelta(seconds=1), {DeliveryChannel.DESKTOP}, fixed_now
    )

    claimed = reminders.claim_due_deliveries(DeliveryChannel.DESKTOP, fixed_now + timedelta(minutes=1), 10)
    assert {item.occurrence_id for item in claimed} == {
        row["id"]
        for row in _occurrences_for_rules(task_database, {first_rule.id, second_rule.id})
    }
    assert all(item.status is DeliveryStatus.SENDING for item in claimed)
    assert len({item.claim_token for item in claimed}) == 2
    assert None not in {item.claim_token for item in claimed}

    recovered = reminders.claim_due_deliveries(
        DeliveryChannel.DESKTOP, fixed_now + timedelta(minutes=6, seconds=1), 10
    )
    assert {item.id for item in recovered} == {item.id for item in claimed}
    assert {item.claim_token for item in recovered}.isdisjoint(
        {item.claim_token for item in claimed}
    )

    delivery = recovered[0]
    assert reminders.mark_delivery_sent(
        delivery.id, "not-the-current-token", fixed_now + timedelta(minutes=7)
    ) is False
    with task_database.connect() as connection:
        unchanged = connection.execute(
            "SELECT status, claim_token FROM notification_deliveries WHERE id = ?", (delivery.id,)
        ).fetchone()
    assert unchanged["status"] == "sending"
    assert unchanged["claim_token"] == delivery.claim_token

    assert reminders.mark_delivery_failed(
        delivery.id,
        delivery.claim_token,
        "smtp_timeout",
        fixed_now + timedelta(minutes=8),
    ) is True
    assert reminders.mark_delivery_skipped(
        recovered[1].id, recovered[1].claim_token, "cancelled"
    ) is True
    with task_database.connect() as connection:
        failed = connection.execute(
            "SELECT status, claim_token, claimed_at, last_error_code, next_attempt_at "
            "FROM notification_deliveries WHERE id = ?",
            (delivery.id,),
        ).fetchone()
    assert dict(failed) == {
        "status": "failed",
        "claim_token": None,
        "claimed_at": None,
        "last_error_code": "smtp_timeout",
        "next_attempt_at": to_utc_text(fixed_now + timedelta(minutes=8)),
    }


def test_cancel_pending_task_reminders_and_caller_transaction_rollback(
    task_database, fixed_now
) -> None:
    tasks = TaskRepository(task_database)
    reminders = ReminderRepository(task_database)
    task = Task.new("可取消", now=fixed_now, due_at=fixed_now + timedelta(days=1))
    tasks.insert(task, event_type="created")
    rule = reminders.create_deadline_rule(task, offset_seconds=-3600, now=fixed_now)
    occurrence = reminders.ensure_occurrence(
        rule, task.due_at - timedelta(hours=1), fixed_now
    )

    reminders.cancel_pending_for_task(task.id, fixed_now + timedelta(minutes=1))
    with task_database.connect() as connection:
        occurrence_row = connection.execute(
            "SELECT status FROM reminder_occurrences WHERE id = ?", (occurrence.id,)
        ).fetchone()
        delivery_rows = connection.execute(
            "SELECT status FROM notification_deliveries WHERE occurrence_id = ?", (occurrence.id,)
        ).fetchall()
    assert occurrence_row["status"] == ReminderOccurrenceStatus.CANCELLED.value
    assert {row["status"] for row in delivery_rows} == {"skipped"}

    rolled_back = Task.new("原子回滚", now=fixed_now, due_at=fixed_now + timedelta(days=2))
    with pytest.raises(RuntimeError, match="rollback"):
        with task_database.transaction() as connection:
            tasks.insert(rolled_back, event_type="created", connection=connection)
            reminders.create_deadline_rule(
                rolled_back, offset_seconds=-7200, now=fixed_now, connection=connection
            )
            raise RuntimeError("rollback")
    assert tasks.get(rolled_back.id) is None
    with task_database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reminder_rules WHERE task_id = ?", (rolled_back.id,)
        ).fetchone()[0] == 0


def _occurrences_for_rules(task_database, rule_ids: set[str]):
    placeholders = ", ".join("?" for _ in rule_ids)
    with task_database.connect() as connection:
        return connection.execute(
            f"SELECT id FROM reminder_occurrences WHERE rule_id IN ({placeholders})",
            tuple(rule_ids),
        ).fetchall()
