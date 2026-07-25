"""任务与提醒的 SQLite 持久化仓储。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator

from app.tasks.database import TaskDatabase
from app.tasks.models import (
    DeliveryChannel,
    DeliveryStatus,
    NotificationDelivery,
    Priority,
    ReminderKind,
    ReminderOccurrence,
    ReminderOccurrenceStatus,
    ReminderRule,
    Task,
    TaskStatus,
    parse_utc,
    to_utc_text,
)


@contextmanager
def _write_connection(
    database: TaskDatabase, connection: sqlite3.Connection | None
) -> Iterator[sqlite3.Connection]:
    """复用调用方事务；未提供时由 TaskDatabase 创建一个事务。"""

    if connection is not None:
        yield connection
        return
    with database.transaction() as owned_connection:
        yield owned_connection


@contextmanager
def _read_connection(database: TaskDatabase) -> Iterator[sqlite3.Connection]:
    """在只读查询完成后显式关闭 SQLite 连接。"""

    connection = database.connect()
    try:
        yield connection
    finally:
        connection.close()


class TaskRepository:
    """保存不可变任务模型及其审计事件。"""

    def __init__(self, database: TaskDatabase) -> None:
        self._database = database

    def insert(
        self,
        task: Task,
        event_type: str,
        connection: sqlite3.Connection | None = None,
    ) -> Task:
        with _write_connection(self._database, connection) as active_connection:
            active_connection.execute(
                """
                INSERT INTO tasks (
                    id, title, details, status, priority, planned_date, due_at,
                    created_at, updated_at, completed_at, cancelled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _task_values(task),
            )
            _insert_task_event(
                active_connection,
                task_id=task.id,
                event_type=event_type,
                before=None,
                after=task,
                occurred_at=task.created_at,
            )
        return task

    def get(self, task_id: str) -> Task | None:
        with _read_connection(self._database) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def find_pending_by_exact_title(self, title: str) -> list[Task]:
        with _read_connection(self._database) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = ? AND title = ?
                ORDER BY due_at IS NULL, due_at, created_at, id
                """,
                (TaskStatus.PENDING.value, title),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def list_pending(self) -> list[Task]:
        with _read_connection(self._database) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = ?
                ORDER BY due_at IS NULL, due_at, created_at, id
                """,
                (TaskStatus.PENDING.value,),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def update(
        self,
        task: Task,
        event_type: str,
        before: Task,
        connection: sqlite3.Connection | None = None,
    ) -> Task:
        with _write_connection(self._database, connection) as active_connection:
            active_connection.execute(
                """
                UPDATE tasks
                SET title = ?, details = ?, status = ?, priority = ?, planned_date = ?,
                    due_at = ?, created_at = ?, updated_at = ?, completed_at = ?,
                    cancelled_at = ?
                WHERE id = ?
                """,
                (*_task_values_without_id(task), task.id),
            )
            active_connection.execute(
                "DELETE FROM task_summary_archives WHERE task_id = ?", (task.id,)
            )
            _insert_task_event(
                active_connection,
                task_id=task.id,
                event_type=event_type,
                before=before,
                after=task,
                occurred_at=task.updated_at,
            )
        return task

    def delete(
        self, task_id: str, connection: sqlite3.Connection | None = None
    ) -> None:
        with _write_connection(self._database, connection) as active_connection:
            row = active_connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return
            task = _task_from_row(row)
            _insert_task_event(
                active_connection,
                task_id=task.id,
                event_type="deleted",
                before=task,
                after=None,
                occurred_at=task.updated_at,
            )
            active_connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def clear_archive_marker(
        self, task_id: str, connection: sqlite3.Connection | None = None
    ) -> None:
        with _write_connection(self._database, connection) as active_connection:
            row = active_connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return
            task = _task_from_row(row)
            active_connection.execute(
                "DELETE FROM task_summary_archives WHERE task_id = ?", (task_id,)
            )
            _insert_task_event(
                active_connection,
                task_id=task.id,
                event_type="archive_cleared",
                before=task,
                after=task,
                occurred_at=task.updated_at,
            )

    def list_events_between(
        self, start: datetime, end: datetime
    ) -> list[dict[str, object]]:
        with _read_connection(self._database) as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, event_type, before_json, after_json, occurred_at
                FROM task_events
                WHERE occurred_at >= ? AND occurred_at <= ?
                ORDER BY occurred_at, id
                """,
                (to_utc_text(start), to_utc_text(end)),
            ).fetchall()
        return [dict(row) for row in rows]


class ReminderRepository:
    """保存提醒规则、实例及基于租约的投递状态。"""

    def __init__(self, database: TaskDatabase) -> None:
        self._database = database

    def create_deadline_rule(
        self,
        task: Task,
        offset_seconds: int,
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> ReminderRule:
        rule = ReminderRule(
            id=str(uuid.uuid4()),
            task_id=task.id,
            message=task.title,
            kind=ReminderKind.DEADLINE_OFFSET,
            offset_seconds=offset_seconds,
            weekdays_mask=None,
            time_of_day=None,
            timezone="Asia/Shanghai",
            grace_seconds=None,
            desktop_enabled=True,
            email_enabled=True,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        with _write_connection(self._database, connection) as active_connection:
            _insert_rule(active_connection, rule)
        return rule

    def create_one_time_rule(
        self,
        message: str,
        scheduled_at: datetime,
        channels: Iterable[DeliveryChannel],
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> ReminderRule:
        desktop_enabled, email_enabled = _channel_flags(channels)
        rule = ReminderRule(
            id=str(uuid.uuid4()),
            task_id=None,
            message=message,
            kind=ReminderKind.ONE_TIME,
            offset_seconds=None,
            weekdays_mask=None,
            time_of_day=None,
            timezone="Asia/Shanghai",
            grace_seconds=None,
            desktop_enabled=desktop_enabled,
            email_enabled=email_enabled,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        with _write_connection(self._database, connection) as active_connection:
            _insert_rule(active_connection, rule)
            _ensure_occurrence(active_connection, rule, scheduled_at, now)
        return rule

    def create_weekly_rule(
        self,
        message: str,
        weekdays_mask: int,
        time_of_day: str,
        grace_seconds: int,
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> ReminderRule:
        rule = ReminderRule(
            id=str(uuid.uuid4()),
            task_id=None,
            message=message,
            kind=ReminderKind.WEEKLY,
            offset_seconds=None,
            weekdays_mask=weekdays_mask,
            time_of_day=time_of_day,
            timezone="Asia/Shanghai",
            grace_seconds=grace_seconds,
            desktop_enabled=True,
            email_enabled=True,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        with _write_connection(self._database, connection) as active_connection:
            _insert_rule(active_connection, rule)
        return rule

    def ensure_occurrence(
        self,
        rule: ReminderRule,
        scheduled_at: datetime,
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> ReminderOccurrence:
        with _write_connection(self._database, connection) as active_connection:
            return _ensure_occurrence(active_connection, rule, scheduled_at, now)

    def cancel_pending_for_task(
        self,
        task_id: str,
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        now_text = to_utc_text(now)
        with _write_connection(self._database, connection) as active_connection:
            active_connection.execute(
                """
                UPDATE reminder_occurrences
                SET status = ?, updated_at = ?
                WHERE task_id = ? AND status = ?
                """,
                (
                    ReminderOccurrenceStatus.CANCELLED.value,
                    now_text,
                    task_id,
                    ReminderOccurrenceStatus.PENDING.value,
                ),
            )
            active_connection.execute(
                """
                UPDATE notification_deliveries
                SET status = ?, claim_token = NULL, claimed_at = NULL,
                    next_attempt_at = NULL, last_error_code = ?
                WHERE occurrence_id IN (
                    SELECT id FROM reminder_occurrences WHERE task_id = ?
                ) AND status IN (?, ?)
                """,
                (
                    DeliveryStatus.SKIPPED.value,
                    "task_cancelled",
                    task_id,
                    DeliveryStatus.PENDING.value,
                    DeliveryStatus.SENDING.value,
                ),
            )

    def disable_deadline_rules_for_task(
        self,
        task_id: str,
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """停用任务的截止时间规则，同时保留规则与投递历史。"""

        with _write_connection(self._database, connection) as active_connection:
            active_connection.execute(
                """
                UPDATE reminder_rules
                SET enabled = 0, updated_at = ?
                WHERE task_id = ? AND kind = ?
                """,
                (to_utc_text(now), task_id, ReminderKind.DEADLINE_OFFSET.value),
            )

    def update_enabled_deadline_rule_messages_for_task(
        self,
        task_id: str,
        message: str,
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """同步仍启用的任务截止提醒文案，不改写停用历史。"""

        with _write_connection(self._database, connection) as active_connection:
            active_connection.execute(
                """
                UPDATE reminder_rules
                SET message = ?, updated_at = ?
                WHERE task_id = ? AND kind = ? AND enabled = 1
                """,
                (
                    message,
                    to_utc_text(now),
                    task_id,
                    ReminderKind.DEADLINE_OFFSET.value,
                ),
            )

    def claim_due_deliveries(
        self,
        channel: DeliveryChannel,
        now: datetime,
        limit: int,
        lease_seconds: int = 300,
    ) -> list[NotificationDelivery]:
        if not isinstance(channel, DeliveryChannel):
            raise TypeError("通知渠道必须是 DeliveryChannel。")
        if limit < 0:
            raise ValueError("领取数量不能为负数。")
        if lease_seconds <= 0:
            raise ValueError("租约秒数必须大于零。")

        now_text = to_utc_text(now)
        lease_cutoff = to_utc_text(now - timedelta(seconds=lease_seconds))
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE notification_deliveries
                SET status = ?, claim_token = NULL, claimed_at = NULL
                WHERE status = ? AND claimed_at <= ?
                """,
                (DeliveryStatus.PENDING.value, DeliveryStatus.SENDING.value, lease_cutoff),
            )
            rows = connection.execute(
                """
                SELECT delivery.*
                FROM notification_deliveries AS delivery
                JOIN reminder_occurrences AS occurrence
                    ON occurrence.id = delivery.occurrence_id
                WHERE delivery.channel = ?
                  AND delivery.status = ?
                  AND (delivery.next_attempt_at IS NULL OR delivery.next_attempt_at <= ?)
                  AND occurrence.status = ?
                  AND occurrence.scheduled_at <= ?
                ORDER BY occurrence.scheduled_at, occurrence.id, delivery.id
                LIMIT ?
                """,
                (
                    channel.value,
                    DeliveryStatus.PENDING.value,
                    now_text,
                    ReminderOccurrenceStatus.PENDING.value,
                    now_text,
                    limit,
                ),
            ).fetchall()
            claimed: list[NotificationDelivery] = []
            for row in rows:
                claim_token = str(uuid.uuid4())
                updated = connection.execute(
                    """
                    UPDATE notification_deliveries
                    SET status = ?, attempt_count = attempt_count + 1,
                        claimed_at = ?, claim_token = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        DeliveryStatus.SENDING.value,
                        now_text,
                        claim_token,
                        row["id"],
                        DeliveryStatus.PENDING.value,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                claimed_row = connection.execute(
                    "SELECT * FROM notification_deliveries WHERE id = ?", (row["id"],)
                ).fetchone()
                if claimed_row is not None:
                    claimed.append(_delivery_from_row(claimed_row))
        return claimed

    def mark_delivery_sent(
        self,
        delivery_id: str,
        claim_token: str,
        sent_at: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        with _write_connection(self._database, connection) as active_connection:
            result = active_connection.execute(
                """
                UPDATE notification_deliveries
                SET status = ?, sent_at = ?, next_attempt_at = NULL,
                    claim_token = NULL, claimed_at = NULL, last_error_code = NULL
                WHERE id = ? AND claim_token = ? AND status = ?
                """,
                (
                    DeliveryStatus.SENT.value,
                    to_utc_text(sent_at),
                    delivery_id,
                    claim_token,
                    DeliveryStatus.SENDING.value,
                ),
            )
        return result.rowcount == 1

    def mark_delivery_failed(
        self,
        delivery_id: str,
        claim_token: str,
        error_code: str,
        next_attempt_at: datetime | None,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        next_attempt_text = (
            to_utc_text(next_attempt_at) if next_attempt_at is not None else None
        )
        status = (
            DeliveryStatus.PENDING
            if next_attempt_text is not None
            else DeliveryStatus.FAILED
        )
        with _write_connection(self._database, connection) as active_connection:
            result = active_connection.execute(
                """
                UPDATE notification_deliveries
                SET status = ?, next_attempt_at = ?, last_error_code = ?,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = ? AND claim_token = ? AND status = ?
                """,
                (
                    status.value,
                    next_attempt_text,
                    error_code,
                    delivery_id,
                    claim_token,
                    DeliveryStatus.SENDING.value,
                ),
            )
        return result.rowcount == 1

    def mark_delivery_skipped(
        self,
        delivery_id: str,
        claim_token: str,
        reason: str,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        with _write_connection(self._database, connection) as active_connection:
            result = active_connection.execute(
                """
                UPDATE notification_deliveries
                SET status = ?, next_attempt_at = NULL, last_error_code = ?,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = ? AND claim_token = ? AND status = ?
                """,
                (
                    DeliveryStatus.SKIPPED.value,
                    reason,
                    delivery_id,
                    claim_token,
                    DeliveryStatus.SENDING.value,
                ),
            )
        return result.rowcount == 1


def _task_values(task: Task) -> tuple[object, ...]:
    return (task.id, *_task_values_without_id(task))


def _task_values_without_id(task: Task) -> tuple[object, ...]:
    return (
        task.title,
        task.details,
        task.status.value,
        task.priority.value,
        task.planned_date.isoformat() if task.planned_date else None,
        to_utc_text(task.due_at) if task.due_at else None,
        to_utc_text(task.created_at),
        to_utc_text(task.updated_at),
        to_utc_text(task.completed_at) if task.completed_at else None,
        to_utc_text(task.cancelled_at) if task.cancelled_at else None,
    )


def _task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        details=row["details"],
        status=TaskStatus(row["status"]),
        priority=Priority(row["priority"]),
        planned_date=date.fromisoformat(row["planned_date"]) if row["planned_date"] else None,
        due_at=parse_utc(row["due_at"]) if row["due_at"] else None,
        created_at=parse_utc(row["created_at"]),
        updated_at=parse_utc(row["updated_at"]),
        completed_at=parse_utc(row["completed_at"]) if row["completed_at"] else None,
        cancelled_at=parse_utc(row["cancelled_at"]) if row["cancelled_at"] else None,
    )


def _task_audit_json(task: Task) -> str:
    data = {
        "cancelled_at": to_utc_text(task.cancelled_at) if task.cancelled_at else None,
        "completed_at": to_utc_text(task.completed_at) if task.completed_at else None,
        "details": task.details,
        "due_at": to_utc_text(task.due_at) if task.due_at else None,
        "planned_date": task.planned_date.isoformat() if task.planned_date else None,
        "priority": task.priority.value,
        "status": task.status.value,
        "title": task.title,
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _insert_task_event(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    event_type: str,
    before: Task | None,
    after: Task | None,
    occurred_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO task_events
            (id, task_id, event_type, before_json, after_json, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            task_id,
            event_type,
            _task_audit_json(before) if before is not None else None,
            _task_audit_json(after) if after is not None else None,
            to_utc_text(occurred_at),
        ),
    )


def _insert_rule(connection: sqlite3.Connection, rule: ReminderRule) -> None:
    connection.execute(
        """
        INSERT INTO reminder_rules (
            id, task_id, message, kind, offset_seconds, weekdays_mask, time_of_day,
            timezone, grace_seconds, desktop_enabled, email_enabled, enabled,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rule.id,
            rule.task_id,
            rule.message,
            rule.kind.value,
            rule.offset_seconds,
            rule.weekdays_mask,
            rule.time_of_day,
            rule.timezone,
            rule.grace_seconds,
            int(rule.desktop_enabled),
            int(rule.email_enabled),
            int(rule.enabled),
            to_utc_text(rule.created_at),
            to_utc_text(rule.updated_at),
        ),
    )


def _ensure_occurrence(
    connection: sqlite3.Connection,
    rule: ReminderRule,
    scheduled_at: datetime,
    now: datetime,
) -> ReminderOccurrence:
    scheduled_text = to_utc_text(scheduled_at)
    now_text = to_utc_text(now)
    expires_at = (
        scheduled_at + timedelta(seconds=rule.grace_seconds)
        if rule.kind is ReminderKind.WEEKLY and rule.grace_seconds is not None
        else None
    )
    connection.execute(
        """
        INSERT INTO reminder_occurrences (
            id, rule_id, task_id, scheduled_at, expires_at, status, skip_reason,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(rule_id, scheduled_at) DO NOTHING
        """,
        (
            str(uuid.uuid4()),
            rule.id,
            rule.task_id,
            scheduled_text,
            to_utc_text(expires_at) if expires_at else None,
            ReminderOccurrenceStatus.PENDING.value,
            now_text,
            now_text,
        ),
    )
    row = connection.execute(
        "SELECT * FROM reminder_occurrences WHERE rule_id = ? AND scheduled_at = ?",
        (rule.id, scheduled_text),
    ).fetchone()
    if row is None:
        raise RuntimeError("提醒实例创建后未找到记录。")
    occurrence = _occurrence_from_row(row)
    _ensure_deliveries(connection, occurrence.id, rule)
    return occurrence


def _ensure_deliveries(
    connection: sqlite3.Connection, occurrence_id: str, rule: ReminderRule
) -> None:
    channels: list[DeliveryChannel] = []
    if rule.desktop_enabled:
        channels.append(DeliveryChannel.DESKTOP)
    if rule.email_enabled:
        channels.append(DeliveryChannel.EMAIL)
    for channel in channels:
        connection.execute(
            """
            INSERT INTO notification_deliveries
                (id, occurrence_id, channel, status, attempt_count)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(occurrence_id, channel) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                occurrence_id,
                channel.value,
                DeliveryStatus.PENDING.value,
            ),
        )


def _rule_from_row(row: sqlite3.Row) -> ReminderRule:
    return ReminderRule(
        id=row["id"],
        task_id=row["task_id"],
        message=row["message"],
        kind=ReminderKind(row["kind"]),
        offset_seconds=row["offset_seconds"],
        weekdays_mask=row["weekdays_mask"],
        time_of_day=row["time_of_day"],
        timezone=row["timezone"],
        grace_seconds=row["grace_seconds"],
        desktop_enabled=bool(row["desktop_enabled"]),
        email_enabled=bool(row["email_enabled"]),
        enabled=bool(row["enabled"]),
        created_at=parse_utc(row["created_at"]),
        updated_at=parse_utc(row["updated_at"]),
    )


def _occurrence_from_row(row: sqlite3.Row) -> ReminderOccurrence:
    return ReminderOccurrence(
        id=row["id"],
        rule_id=row["rule_id"],
        task_id=row["task_id"],
        scheduled_at=parse_utc(row["scheduled_at"]),
        expires_at=parse_utc(row["expires_at"]) if row["expires_at"] else None,
        status=ReminderOccurrenceStatus(row["status"]),
        skip_reason=row["skip_reason"],
        created_at=parse_utc(row["created_at"]),
        updated_at=parse_utc(row["updated_at"]),
    )


def _delivery_from_row(row: sqlite3.Row) -> NotificationDelivery:
    return NotificationDelivery(
        id=row["id"],
        occurrence_id=row["occurrence_id"],
        channel=DeliveryChannel(row["channel"]),
        status=DeliveryStatus(row["status"]),
        attempt_count=row["attempt_count"],
        next_attempt_at=parse_utc(row["next_attempt_at"])
        if row["next_attempt_at"]
        else None,
        claimed_at=parse_utc(row["claimed_at"]) if row["claimed_at"] else None,
        claim_token=row["claim_token"],
        sent_at=parse_utc(row["sent_at"]) if row["sent_at"] else None,
        last_error_code=row["last_error_code"],
    )


def _channel_flags(channels: Iterable[DeliveryChannel]) -> tuple[bool, bool]:
    selected = set(channels)
    if any(not isinstance(channel, DeliveryChannel) for channel in selected):
        raise TypeError("通知渠道必须是 DeliveryChannel。")
    return (
        DeliveryChannel.DESKTOP in selected,
        DeliveryChannel.EMAIL in selected,
    )
