from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.paths import StoragePaths
from app.tasks.models import (
    DeliveryStatus,
    DeliveryChannel,
    NotificationDelivery,
    Priority,
    ReminderOccurrence,
    ReminderOccurrenceStatus,
    ReminderKind,
    ReminderRule,
    Task,
    TaskStatus,
    WeeklySummaryRun,
    WeeklySummaryStatus,
    ensure_utc,
    parse_utc,
    to_utc_text,
)
from app.tasks.settings import TaskAssistantSettings


def test_task_defaults_and_utc_round_trip(tmp_path: Path) -> None:
    now = datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)
    task = Task.new("完成实验报告", now=now)
    assert task.status is TaskStatus.PENDING
    assert task.priority is Priority.NORMAL
    assert parse_utc(to_utc_text(now)) == now

    paths = StoragePaths(tmp_path)
    assert paths.tasks_database() == tmp_path / "data" / "tasks.db"
    assert paths.task_assistant_config() == tmp_path / "data" / "config" / "task_assistant.json"
    assert paths.task_database_backup_dir == tmp_path / "data" / "backups" / "sqlite"


def test_task_assistant_settings_are_non_secret() -> None:
    settings = TaskAssistantSettings()
    data = settings.to_dict()
    assert data["timezone"] == "Asia/Shanghai"
    assert data["smtp_host"] == "smtp.qq.com"
    assert "password" not in data
    assert "api_key" not in data


def test_task_assistant_settings_loads_invalid_utf8_as_defaults(tmp_path: Path) -> None:
    path = tmp_path / "data" / "config" / "task_assistant.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")

    assert TaskAssistantSettings.load(path) == TaskAssistantSettings()


def test_task_new_normalizes_text_and_priority(fixed_now: datetime) -> None:
    task = Task.new(" x ", now=fixed_now, details=" y ", priority=Priority.HIGH)

    assert task.title == "x"
    assert task.details == "y"
    assert task.priority is Priority.HIGH


@pytest.mark.parametrize(
    ("title", "details", "priority"),
    [
        (123, "", Priority.NORMAL),
        ("x", 123, Priority.NORMAL),
        ("x", "", "high"),
    ],
)
def test_task_new_rejects_invalid_types(
    fixed_now: datetime,
    title: object,
    details: object,
    priority: object,
) -> None:
    with pytest.raises(TypeError):
        Task.new(title, now=fixed_now, details=details, priority=priority)


def test_task_new_rejects_blank_title(fixed_now: datetime) -> None:
    with pytest.raises(ValueError, match="任务标题不能为空。"):
        Task.new("   ", now=fixed_now)


def test_weekly_summary_status_is_typed_and_rejects_bare_string(fixed_now: datetime) -> None:
    assert [status.value for status in WeeklySummaryStatus] == [
        "pending",
        "generating",
        "awaiting_approval",
        "publishing",
        "published",
        "cleaned",
        "failed",
    ]
    run = WeeklySummaryRun(
        id="summary-1",
        iso_year=2026,
        iso_week=30,
        week_start=fixed_now.date(),
        week_end=fixed_now.date(),
        status=WeeklySummaryStatus.AWAITING_APPROVAL,
        provider=None,
        snapshot_sha256=None,
        draft_path=None,
        git_commit_sha=None,
        last_error_code=None,
        created_at=fixed_now,
        generated_at=None,
        approved_at=None,
        published_at=None,
        cleaned_at=None,
    )

    assert run.status is WeeklySummaryStatus.AWAITING_APPROVAL
    failed_run = WeeklySummaryRun(
        id="summary-3",
        iso_year=2026,
        iso_week=30,
        week_start=fixed_now.date(),
        week_end=fixed_now.date(),
        status=WeeklySummaryStatus.FAILED,
        provider=None,
        snapshot_sha256=None,
        draft_path=None,
        git_commit_sha=None,
        last_error_code="generation_failed",
        created_at=fixed_now,
        generated_at=None,
        approved_at=None,
        published_at=None,
        cleaned_at=None,
    )
    assert failed_run.status is WeeklySummaryStatus.FAILED
    with pytest.raises(TypeError):
        WeeklySummaryRun(
            id="summary-2",
            iso_year=2026,
            iso_week=30,
            week_start=fixed_now.date(),
            week_end=fixed_now.date(),
            status="awaiting_approval",
            provider=None,
            snapshot_sha256=None,
            draft_path=None,
            git_commit_sha=None,
            last_error_code=None,
            created_at=fixed_now,
            generated_at=None,
            approved_at=None,
            published_at=None,
            cleaned_at=None,
        )


def test_utc_helpers_normalize_aware_datetimes_and_reject_naive() -> None:
    local_time = datetime(2026, 7, 25, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    expected = datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)

    assert ensure_utc(local_time) == expected
    assert parse_utc(to_utc_text(local_time)) == expected
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 7, 25, 4, 0))


def test_utc_text_is_fixed_width_and_orders_fractional_seconds() -> None:
    whole_second = datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)
    half_second_later = whole_second + timedelta(microseconds=500_000)

    assert to_utc_text(whole_second) == "2026-07-25T04:00:00.000000Z"
    assert to_utc_text(half_second_later) == "2026-07-25T04:00:00.500000Z"
    assert to_utc_text(whole_second) < to_utc_text(half_second_later)
    assert parse_utc(to_utc_text(whole_second)) == whole_second
    assert parse_utc(to_utc_text(half_second_later)) == half_second_later


def test_reminder_occurrence_normalizes_datetimes_and_rejects_naive() -> None:
    local_time = datetime(2026, 7, 25, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    occurrence = ReminderOccurrence(
        id="occurrence-1",
        rule_id="rule-1",
        task_id=None,
        scheduled_at=local_time,
        expires_at=None,
        status=ReminderOccurrenceStatus.PENDING,
        skip_reason=None,
        created_at=local_time,
        updated_at=local_time,
    )

    assert occurrence.scheduled_at == datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        ReminderOccurrence(
            id="occurrence-2",
            rule_id="rule-1",
            task_id=None,
            scheduled_at=datetime(2026, 7, 25, 4, 0),
            expires_at=None,
            status=ReminderOccurrenceStatus.PENDING,
            skip_reason=None,
            created_at=local_time,
            updated_at=local_time,
        )

    with pytest.raises(TypeError):
        ReminderOccurrence(
            id="occurrence-3",
            rule_id="rule-1",
            task_id=None,
            scheduled_at=local_time,
            expires_at=None,
            status="pending",
            skip_reason=None,
            created_at=local_time,
            updated_at=local_time,
        )


def test_task_direct_constructor_enforces_status_and_priority_enums(fixed_now: datetime) -> None:
    task = Task(
        id="task-1",
        title="任务",
        details="",
        status=TaskStatus.PENDING,
        priority=Priority.NORMAL,
        planned_date=None,
        due_at=None,
        created_at=fixed_now,
        updated_at=fixed_now,
        completed_at=None,
        cancelled_at=None,
    )

    assert task.status is TaskStatus.PENDING
    assert task.priority is Priority.NORMAL
    with pytest.raises(TypeError):
        Task(
            id="task-2",
            title="任务",
            details="",
            status="pending",
            priority=Priority.NORMAL,
            planned_date=None,
            due_at=None,
            created_at=fixed_now,
            updated_at=fixed_now,
            completed_at=None,
            cancelled_at=None,
        )
    with pytest.raises(TypeError):
        Task(
            id="task-3",
            title="任务",
            details="",
            status=TaskStatus.PENDING,
            priority="normal",
            planned_date=None,
            due_at=None,
            created_at=fixed_now,
            updated_at=fixed_now,
            completed_at=None,
            cancelled_at=None,
        )


def test_reminder_rule_direct_constructor_enforces_kind_enum(fixed_now: datetime) -> None:
    rule = ReminderRule(
        id="rule-1",
        task_id=None,
        message="提醒",
        kind=ReminderKind.ONE_TIME,
        offset_seconds=None,
        weekdays_mask=None,
        time_of_day=None,
        timezone="Asia/Shanghai",
        grace_seconds=None,
        desktop_enabled=True,
        email_enabled=False,
        enabled=True,
        created_at=fixed_now,
        updated_at=fixed_now,
    )

    assert rule.kind is ReminderKind.ONE_TIME
    with pytest.raises(TypeError):
        ReminderRule(
            id="rule-2",
            task_id=None,
            message="提醒",
            kind="one_time",
            offset_seconds=None,
            weekdays_mask=None,
            time_of_day=None,
            timezone="Asia/Shanghai",
            grace_seconds=None,
            desktop_enabled=True,
            email_enabled=False,
            enabled=True,
            created_at=fixed_now,
            updated_at=fixed_now,
        )


def test_notification_delivery_direct_constructor_enforces_channel_and_status_enums(
    fixed_now: datetime,
) -> None:
    delivery = NotificationDelivery(
        id="delivery-1",
        occurrence_id="occurrence-1",
        channel=DeliveryChannel.DESKTOP,
        status=DeliveryStatus.PENDING,
        attempt_count=0,
        next_attempt_at=fixed_now,
        claimed_at=None,
        claim_token=None,
        sent_at=None,
        last_error_code=None,
    )

    assert delivery.channel is DeliveryChannel.DESKTOP
    assert delivery.status is DeliveryStatus.PENDING
    with pytest.raises(TypeError):
        NotificationDelivery(
            id="delivery-2",
            occurrence_id="occurrence-1",
            channel="desktop",
            status=DeliveryStatus.PENDING,
            attempt_count=0,
            next_attempt_at=fixed_now,
            claimed_at=None,
            claim_token=None,
            sent_at=None,
            last_error_code=None,
        )
    with pytest.raises(TypeError):
        NotificationDelivery(
            id="delivery-3",
            occurrence_id="occurrence-1",
            channel=DeliveryChannel.DESKTOP,
            status="pending",
            attempt_count=0,
            next_attempt_at=fixed_now,
            claimed_at=None,
            claim_token=None,
            sent_at=None,
            last_error_code=None,
        )


def test_storage_paths_ensure_dirs_creates_task_directories_not_database(tmp_path: Path) -> None:
    paths = StoragePaths(tmp_path)

    paths.ensure_dirs()

    assert paths.config_dir.is_dir()
    assert paths.task_database_backup_dir.is_dir()
    assert paths.weekly_summary_drafts_dir.is_dir()
    assert paths.weekly_summary_snapshots_dir.is_dir()
    assert not paths.tasks_database().exists()


def test_task_assistant_settings_save_and_load_are_non_secret(tmp_path: Path) -> None:
    path = tmp_path / "data" / "config" / "task_assistant.json"
    settings = TaskAssistantSettings(
        email_enabled=True,
        qq_email="sender@example.com",
        recipient_email="recipient@example.com",
    )

    settings.save(path)

    assert TaskAssistantSettings.load(path) == settings
    text = path.read_text(encoding="utf-8")
    assert "password" not in text
    assert "api_key" not in text


def test_task_assistant_settings_from_dict_uses_defaults_for_invalid_types() -> None:
    settings = TaskAssistantSettings.from_dict(
        {
            "timezone": 8,
            "email_enabled": "yes",
            "smtp_port": "465",
            "summary_enabled": 1,
            "deepseek_model": None,
        }
    )

    assert settings == TaskAssistantSettings()
