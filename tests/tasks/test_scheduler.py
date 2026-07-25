from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread
from zoneinfo import ZoneInfo

import pytest

from app.tasks.models import (
    DeliveryChannel,
    DeliveryStatus,
    ReminderKind,
    ReminderOccurrenceStatus,
    to_utc_text,
)
from app.tasks.repository import ReminderRepository, TaskRepository
from app.tasks.scheduler import ReminderScheduler
from app.tasks.service import TaskService


SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def reminders(task_database) -> ReminderRepository:
    return ReminderRepository(task_database)


@pytest.fixture
def task_service(task_database, reminders, fixed_now) -> TaskService:
    return TaskService(
        task_database,
        TaskRepository(task_database),
        reminders,
        clock=lambda: fixed_now,
    )


@pytest.fixture
def scheduler(reminders) -> ReminderScheduler:
    return ReminderScheduler(reminders)


def shanghai(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def test_scheduler_coalesces_missed_deadline_offsets(
    scheduler, task_service, fixed_now
) -> None:
    task = task_service.create_task(
        title="报告",
        due_at="2026-07-26T04:00:00Z",
        now=fixed_now - timedelta(days=2),
    )

    claimed = scheduler.claim_due(
        "email", now=fixed_now + timedelta(days=2), limit=10
    )

    assert len(claimed) == 1
    delivery = claimed[0]
    assert delivery.channel is DeliveryChannel.EMAIL
    assert delivery.task_id == task.id
    assert delivery.coalesced_count == 2
    assert delivery.scheduled_at == task.due_at - timedelta(hours=2)
    assert delivery.claim_token
    assert "报告" in delivery.display_text
    assert "原计划提醒时间" in delivery.display_text
    assert "已逾期" in delivery.display_text
    assert "合并" in delivery.display_text
    assert delivery.speech_text.startswith("提醒：报告")

    with task_service.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT occurrence.status AS occurrence_status,
                   occurrence.skip_reason AS skip_reason,
                   delivery.status AS delivery_status,
                   delivery.last_error_code AS last_error_code
            FROM reminder_occurrences AS occurrence
            JOIN notification_deliveries AS delivery
                ON delivery.occurrence_id = occurrence.id
            WHERE occurrence.task_id = ? AND delivery.channel = ?
            ORDER BY occurrence.scheduled_at
            """,
            (task.id, DeliveryChannel.EMAIL.value),
        ).fetchall()
    assert [(row["occurrence_status"], row["skip_reason"]) for row in rows] == [
        (ReminderOccurrenceStatus.PENDING.value, None),
        (ReminderOccurrenceStatus.PENDING.value, None),
    ]
    assert [row["delivery_status"] for row in rows] == [
        DeliveryStatus.SKIPPED.value,
        DeliveryStatus.SENDING.value,
    ]
    assert rows[0]["last_error_code"] == "coalesced"


def test_email_coalescing_keeps_pending_desktop_channel_claimable(
    scheduler, task_service, fixed_now
) -> None:
    task = task_service.create_task(
        "双渠道补发",
        due_at=fixed_now + timedelta(days=1),
        now=fixed_now - timedelta(days=2),
    )
    claim_at = fixed_now + timedelta(days=2)

    email = scheduler.claim_due("email", now=claim_at, limit=10)

    assert len(email) == 1
    assert email[0].coalesced_count == 2
    with task_service.database.connect() as connection:
        older = connection.execute(
            """
            SELECT occurrence.status AS occurrence_status,
                   email.status AS email_status,
                   desktop.status AS desktop_status
            FROM reminder_occurrences AS occurrence
            JOIN notification_deliveries AS email
                ON email.occurrence_id = occurrence.id AND email.channel = 'email'
            JOIN notification_deliveries AS desktop
                ON desktop.occurrence_id = occurrence.id AND desktop.channel = 'desktop'
            WHERE occurrence.task_id = ?
            ORDER BY occurrence.scheduled_at
            LIMIT 1
            """,
            (task.id,),
        ).fetchone()
    assert dict(older) == {
        "occurrence_status": ReminderOccurrenceStatus.PENDING.value,
        "email_status": DeliveryStatus.SKIPPED.value,
        "desktop_status": DeliveryStatus.PENDING.value,
    }

    desktop = scheduler.claim_due("desktop", now=claim_at, limit=10)

    assert len(desktop) == 1
    assert desktop[0].coalesced_count == 2
    with task_service.database.connect() as connection:
        final_occurrence = connection.execute(
            """
            SELECT status, skip_reason FROM reminder_occurrences
            WHERE task_id = ?
            ORDER BY scheduled_at
            LIMIT 1
            """,
            (task.id,),
        ).fetchone()
    assert dict(final_occurrence) == {
        "status": ReminderOccurrenceStatus.SKIPPED.value,
        "skip_reason": "coalesced",
    }


def test_coalesced_occurrence_settles_after_other_channel_is_sent(
    scheduler, task_service, reminders, fixed_now
) -> None:
    desktop_delivery, occurrence_id, claim_at = _email_coalesced_while_desktop_sending(
        scheduler, task_service, reminders, fixed_now
    )

    assert reminders.mark_delivery_sent(
        desktop_delivery.id,
        desktop_delivery.claim_token,
        claim_at + timedelta(seconds=1),
    )

    assert _occurrence_state(task_service, occurrence_id) == (
        ReminderOccurrenceStatus.SKIPPED.value,
        "coalesced",
    )


def test_coalesced_occurrence_settles_after_other_channel_permanently_fails(
    scheduler, task_service, reminders, fixed_now
) -> None:
    desktop_delivery, occurrence_id, _ = _email_coalesced_while_desktop_sending(
        scheduler, task_service, reminders, fixed_now
    )

    assert reminders.mark_delivery_failed(
        desktop_delivery.id,
        desktop_delivery.claim_token,
        "permanent",
        None,
    )

    assert _occurrence_state(task_service, occurrence_id) == (
        ReminderOccurrenceStatus.SKIPPED.value,
        "coalesced",
    )


def test_retry_pending_delivery_does_not_settle_coalesced_occurrence(
    scheduler, task_service, reminders, fixed_now
) -> None:
    desktop_delivery, occurrence_id, claim_at = _email_coalesced_while_desktop_sending(
        scheduler, task_service, reminders, fixed_now
    )

    assert reminders.mark_delivery_failed(
        desktop_delivery.id,
        desktop_delivery.claim_token,
        "temporary",
        claim_at + timedelta(minutes=10),
    )

    assert _occurrence_state(task_service, occurrence_id) == (
        ReminderOccurrenceStatus.PENDING.value,
        None,
    )


def test_normal_occurrence_completes_only_after_all_channels_are_sent(
    scheduler, reminders, task_database, fixed_now
) -> None:
    rule = reminders.create_one_time_rule(
        "双渠道正常提醒",
        fixed_now,
        channels=(DeliveryChannel.DESKTOP, DeliveryChannel.EMAIL),
        now=fixed_now,
    )
    desktop = scheduler.claim_due("desktop", now=fixed_now, limit=1)[0]
    email = scheduler.claim_due("email", now=fixed_now, limit=1)[0]

    assert reminders.mark_delivery_sent(desktop.delivery_id, desktop.claim_token, fixed_now)
    with task_database.connect() as connection:
        pending = connection.execute(
            "SELECT status FROM reminder_occurrences WHERE rule_id = ?", (rule.id,)
        ).fetchone()
    assert pending["status"] == ReminderOccurrenceStatus.PENDING.value

    assert reminders.mark_delivery_sent(email.delivery_id, email.claim_token, fixed_now)
    with task_database.connect() as connection:
        completed = connection.execute(
            "SELECT status, skip_reason FROM reminder_occurrences WHERE rule_id = ?",
            (rule.id,),
        ).fetchone()
    assert dict(completed) == {
        "status": ReminderOccurrenceStatus.COMPLETED.value,
        "skip_reason": None,
    }


def test_weekly_occurrence_expires_after_thirty_minutes(scheduler) -> None:
    scheduler.ensure_stock_rule()

    before = scheduler.claim_due("email", now=shanghai("2026-07-27 14:29"), limit=10)
    after = scheduler.claim_due("email", now=shanghai("2026-07-28 14:31"), limit=10)

    assert len(before) == 1
    assert before[0].message.startswith("请检查今天的个人计划")
    assert after == []
    assert scheduler.skipped_reason_for("2026-07-28") == "expired"


def test_generation_is_idempotent_and_one_time_is_not_rebuilt(
    scheduler, reminders, fixed_now, task_database
) -> None:
    stock = scheduler.ensure_stock_rule()
    assert scheduler.ensure_stock_rule() == stock
    one_time = reminders.create_one_time_rule(
        "一次提醒",
        fixed_now + timedelta(hours=1),
        channels=(DeliveryChannel.DESKTOP,),
        now=fixed_now,
    )

    monday_before_stock_check = shanghai("2026-07-27 13:00")
    scheduler.generate_occurrences(now=monday_before_stock_check)
    scheduler.generate_occurrences(now=monday_before_stock_check)

    with task_database.connect() as connection:
        stock_rules = connection.execute(
            "SELECT COUNT(*) FROM reminder_rules WHERE kind = ?",
            (ReminderKind.WEEKLY.value,),
        ).fetchone()[0]
        stock_occurrences = connection.execute(
            "SELECT COUNT(*) FROM reminder_occurrences WHERE rule_id = ?",
            (stock.id,),
        ).fetchone()[0]
        one_time_occurrences = connection.execute(
            "SELECT COUNT(*) FROM reminder_occurrences WHERE rule_id = ?",
            (one_time.id,),
        ).fetchone()[0]
    assert stock_rules == 1
    assert stock_occurrences == 1
    assert one_time_occurrences == 1


def test_deadline_reminder_does_not_expire_after_thirty_minutes(
    scheduler, task_service, fixed_now
) -> None:
    task = task_service.create_task(
        "不会过期的截止提醒",
        due_at=fixed_now + timedelta(days=1),
        now=fixed_now,
    )

    claimed = scheduler.claim_due(
        DeliveryChannel.DESKTOP,
        now=task.due_at - timedelta(hours=2) + timedelta(minutes=31),
        limit=10,
    )

    assert len(claimed) == 1
    assert claimed[0].task_id == task.id


def test_sent_channel_is_never_reclaimed_or_coalesced(
    scheduler, task_service, reminders, fixed_now
) -> None:
    task = task_service.create_task(
        "已发渠道",
        due_at=fixed_now + timedelta(days=2),
        now=fixed_now - timedelta(days=2),
    )
    first_due = task.due_at - timedelta(days=1)
    first = scheduler.claim_due(DeliveryChannel.EMAIL, now=first_due, limit=10)
    assert len(first) == 1
    assert reminders.mark_delivery_sent(
        first[0].delivery_id, first[0].claim_token, first_due
    )

    later = scheduler.claim_due(
        DeliveryChannel.EMAIL, now=task.due_at + timedelta(hours=1), limit=10
    )

    assert len(later) == 1
    assert later[0].coalesced_count == 1
    with task_service.database.connect() as connection:
        sent = connection.execute(
            "SELECT status FROM notification_deliveries WHERE id = ?",
            (first[0].delivery_id,),
        ).fetchone()
    assert sent["status"] == DeliveryStatus.SENT.value


def test_expired_lease_is_recovered_before_deadline_coalescing(
    scheduler, task_service, fixed_now
) -> None:
    task = task_service.create_task(
        "崩溃后的补发",
        due_at=fixed_now + timedelta(days=2),
        now=fixed_now,
    )
    first_scheduled_at = task.due_at - timedelta(days=1)
    first_claim = scheduler.claim_due("email", now=first_scheduled_at, limit=1)
    assert len(first_claim) == 1

    recovered = scheduler.claim_due(
        "email", now=task.due_at - timedelta(hours=2), limit=10
    )

    assert len(recovered) == 1
    assert recovered[0].coalesced_count == 2
    assert recovered[0].delivery_id != first_claim[0].delivery_id


def test_channels_are_claimed_independently_and_limit_is_enforced(
    scheduler, reminders, fixed_now
) -> None:
    reminders.create_one_time_rule(
        "双渠道",
        fixed_now,
        channels=(DeliveryChannel.DESKTOP, DeliveryChannel.EMAIL),
        now=fixed_now,
    )

    desktop = scheduler.claim_due("desktop", now=fixed_now, limit=1)
    email = scheduler.claim_due("email", now=fixed_now, limit=1)

    assert len(desktop) == 1
    assert len(email) == 1
    assert desktop[0].delivery_id != email[0].delivery_id
    assert desktop[0].channel is DeliveryChannel.DESKTOP
    assert email[0].channel is DeliveryChannel.EMAIL
    assert scheduler.claim_due("desktop", now=fixed_now, limit=0) == []
    with pytest.raises(ValueError, match="数量"):
        scheduler.claim_due("desktop", now=fixed_now, limit=-1)
    with pytest.raises(ValueError, match="渠道"):
        scheduler.claim_due("sms", now=fixed_now, limit=1)


def test_concurrent_schedulers_do_not_claim_the_same_delivery(
    reminders, fixed_now
) -> None:
    reminders.create_one_time_rule(
        "并发领取",
        fixed_now,
        channels=(DeliveryChannel.DESKTOP,),
        now=fixed_now,
    )
    first = ReminderScheduler(reminders)
    second = ReminderScheduler(reminders)
    barrier = Barrier(3)
    results: list[list[object]] = []
    errors: list[BaseException] = []

    def claim(scheduler: ReminderScheduler) -> None:
        try:
            barrier.wait()
            results.append(scheduler.claim_due("desktop", now=fixed_now, limit=1))
        except BaseException as exc:  # pragma: no cover - failure is asserted below
            errors.append(exc)

    threads = [Thread(target=claim, args=(scheduler,)) for scheduler in (first, second)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    claimed = [delivery for result in results for delivery in result]
    assert len(claimed) == 1
    assert claimed[0].channel is DeliveryChannel.DESKTOP


def test_rule_and_occurrence_uniqueness_hold_under_repeated_generation(
    scheduler, task_service, fixed_now
) -> None:
    task = task_service.create_task(
        "幂等截止提醒",
        due_at=fixed_now + timedelta(days=1),
        now=fixed_now,
    )

    scheduler.generate_occurrences(now=fixed_now)
    scheduler.generate_occurrences(now=fixed_now)

    with task_service.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT rule_id, scheduled_at, COUNT(*) AS count
            FROM reminder_occurrences
            WHERE task_id = ?
            GROUP BY rule_id, scheduled_at
            """,
            (task.id,),
        ).fetchall()
    assert len(rows) == 2
    assert {row["count"] for row in rows} == {1}


def test_scheduler_marks_weekly_expiry_without_touching_sent_delivery(
    scheduler, reminders, fixed_now, task_database
) -> None:
    rule = reminders.create_weekly_rule(
        "保留已发送",
        weekdays_mask=0b0000010,
        time_of_day="12:00:00",
        grace_seconds=1800,
        now=fixed_now,
    )
    monday_at_noon = shanghai("2026-07-27 12:00")
    scheduler.generate_occurrences(now=monday_at_noon)
    claimed = scheduler.claim_due("desktop", now=monday_at_noon, limit=1)
    assert len(claimed) == 1
    assert reminders.mark_delivery_sent(
        claimed[0].delivery_id, claimed[0].claim_token, monday_at_noon)

    scheduler.claim_due("desktop", now=monday_at_noon + timedelta(minutes=31), limit=1)

    with task_database.connect() as connection:
        occurrence = connection.execute(
            "SELECT status, skip_reason FROM reminder_occurrences WHERE rule_id = ?",
            (rule.id,),
        ).fetchone()
        delivery = connection.execute(
            """
            SELECT status FROM notification_deliveries
            WHERE occurrence_id = (SELECT id FROM reminder_occurrences WHERE rule_id = ?)
              AND channel = ?
            """,
            (rule.id, DeliveryChannel.DESKTOP.value),
        ).fetchone()
    assert occurrence["status"] == ReminderOccurrenceStatus.SKIPPED.value
    assert occurrence["skip_reason"] == "expired"
    assert delivery["status"] == DeliveryStatus.SENT.value


def test_prepared_delivery_keeps_original_utc_time(scheduler, reminders, fixed_now) -> None:
    scheduled_at = fixed_now - timedelta(minutes=1)
    reminders.create_one_time_rule(
        "保留计划时间",
        scheduled_at,
        channels=(DeliveryChannel.DESKTOP,),
        now=fixed_now - timedelta(hours=1),
    )

    claimed = scheduler.claim_due("desktop", now=fixed_now, limit=1)

    assert len(claimed) == 1
    assert claimed[0].scheduled_at == scheduled_at
    assert to_utc_text(scheduled_at)[:16].replace("T", " ") not in claimed[0].display_text
    assert "2026-07-25 11:59" in claimed[0].display_text


def _email_coalesced_while_desktop_sending(
    scheduler: ReminderScheduler,
    task_service: TaskService,
    reminders: ReminderRepository,
    fixed_now: datetime,
):
    task = task_service.create_task(
        "跨渠道终态",
        due_at=fixed_now + timedelta(days=1),
        now=fixed_now - timedelta(days=2),
    )
    claim_at = fixed_now + timedelta(days=2)
    desktop_delivery = reminders.claim_due_deliveries(
        DeliveryChannel.DESKTOP, claim_at, limit=1
    )[0]
    email = scheduler.claim_due("email", now=claim_at, limit=10)
    assert len(email) == 1
    with task_service.database.connect() as connection:
        occurrence_id = connection.execute(
            """
            SELECT occurrence.id
            FROM reminder_occurrences AS occurrence
            JOIN notification_deliveries AS desktop
                ON desktop.occurrence_id = occurrence.id AND desktop.channel = 'desktop'
            WHERE occurrence.task_id = ? AND desktop.id = ?
            """,
            (task.id, desktop_delivery.id),
        ).fetchone()["id"]
    return desktop_delivery, occurrence_id, claim_at


def _occurrence_state(task_service: TaskService, occurrence_id: str) -> tuple[str, str | None]:
    with task_service.database.connect() as connection:
        row = connection.execute(
            "SELECT status, skip_reason FROM reminder_occurrences WHERE id = ?",
            (occurrence_id,),
        ).fetchone()
    return row["status"], row["skip_reason"]
