"""任务生命周期的单事务应用服务。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone, timedelta
from typing import TYPE_CHECKING, Callable, Final, TypeAlias

from app.tasks.database import TaskDatabase
from app.tasks.errors import ConfirmationRequired, TaskAssistantError
from app.tasks.models import Priority, Task, TaskStatus, ensure_utc, parse_utc
from app.tasks.repository import ReminderRepository, TaskRepository, _task_from_row

if TYPE_CHECKING:
    from app.tasks.today_query import TodayQueryResult


class TaskNotFoundError(TaskAssistantError):
    """请求的任务不存在。"""


class AmbiguousTaskReferenceError(ConfirmationRequired):
    """标题匹配到多个任务，调用方需要提供明确 ID。"""

    def __init__(self, candidates: tuple[Task, ...]) -> None:
        self.candidates = tuple(candidates)
        super().__init__("任务标题不唯一，请提供任务 ID。")


class InvalidTaskTransitionError(TaskAssistantError):
    """任务状态变更不符合状态机。"""


_UNSET: Final = object()
_DEFAULT_DEADLINE_OFFSETS: Final = (-86400, -7200)
Clock: TypeAlias = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskService:
    """协调任务、审计事件与提醒的应用服务。"""

    def __init__(
        self,
        database: TaskDatabase,
        tasks: TaskRepository,
        reminders: ReminderRepository,
        clock: Clock = _utc_now,
    ) -> None:
        self.database = database
        self.tasks = tasks
        self.reminders = reminders
        self._clock = clock

    def create_task(
        self,
        title: str,
        *,
        details: str = "",
        priority: Priority | str = Priority.NORMAL,
        planned_date: date | str | None = None,
        due_at: datetime | str | None = None,
        allow_past_due: bool = False,
        now: datetime | None = None,
    ) -> Task:
        occurred_at = self._now(now)
        normalized_due_at = _coerce_due_at(due_at)
        if normalized_due_at is not None and normalized_due_at <= occurred_at:
            if not allow_past_due:
                raise ConfirmationRequired("截止时间已过去，需要确认后才能创建任务。")

        task = Task.new(
            title,
            details=details,
            priority=_coerce_priority(priority),
            planned_date=_coerce_planned_date(planned_date),
            due_at=normalized_due_at,
            now=occurred_at,
        )
        with self.database.transaction(immediate=True) as connection:
            self.tasks.insert(task, event_type="created", connection=connection)
            self._create_default_deadline_reminders(task, occurred_at, connection)
        return task

    def get_task(self, task_id: str) -> Task | None:
        """按唯一 ID 取得任务；不存在时返回 ``None``。"""

        return self.tasks.get(task_id)

    def resolve_task(self, reference: str) -> Task:
        """安全地将唯一 ID 或唯一精确标题解析为任务。"""

        if not isinstance(reference, str):
            raise TaskNotFoundError("任务不存在。")
        by_id = self.tasks.get(reference)
        if by_id is not None:
            return by_id

        candidates = self._tasks_with_exact_title(reference)
        if not candidates:
            raise TaskNotFoundError("任务不存在。")
        if len(candidates) != 1:
            raise AmbiguousTaskReferenceError(candidates)
        return candidates[0]

    def list_pending_tasks(self) -> tuple[Task, ...]:
        """提供今日查询等只读调用使用的待办任务快照。"""

        return tuple(self.tasks.list_pending())

    def query_today(self, now: datetime) -> TodayQueryResult:
        """委托今日查询服务，避免在生命周期服务中复制查询规则。"""

        from app.tasks.today_query import TodayQueryService

        return TodayQueryService(self).query(now)

    def update_task(
        self,
        reference: str,
        *,
        title: str | None = None,
        details: str | None = None,
        priority: Priority | str | None = None,
        planned_date: date | str | None | object = _UNSET,
        due_at: datetime | str | None | object = _UNSET,
        status: TaskStatus | str | object = _UNSET,
        allow_past_due: bool = False,
        now: datetime | None = None,
    ) -> Task:
        if status is not _UNSET:
            raise InvalidTaskTransitionError(
                "任务状态只能通过完成、取消或重新打开操作变更。"
            )

        occurred_at = self._now(now)
        due_changed = due_at is not _UNSET
        with self.database.transaction(immediate=True) as connection:
            before = self._resolve_task_in_connection(reference, connection)
            normalized_due_at = (
                _coerce_due_at(due_at) if due_changed else before.due_at
            )
            if (
                due_changed
                and normalized_due_at is not None
                and normalized_due_at <= occurred_at
                and not allow_past_due
            ):
                raise ConfirmationRequired("截止时间已过去，需要确认后才能更新任务。")
            updated = replace(
                before,
                title=_coerce_title(title) if title is not None else before.title,
                details=details.strip() if details is not None else before.details,
                priority=(
                    _coerce_priority(priority) if priority is not None else before.priority
                ),
                planned_date=(
                    _coerce_planned_date(planned_date)
                    if planned_date is not _UNSET
                    else before.planned_date
                ),
                due_at=normalized_due_at,
                updated_at=occurred_at,
            )
            if due_changed:
                self.reminders.cancel_pending_for_task(
                    before.id, occurred_at, connection=connection
                )
                self.reminders.disable_deadline_rules_for_task(
                    before.id, occurred_at, connection=connection
                )
            self.tasks.update(
                updated, event_type="updated", before=before, connection=connection
            )
            if not due_changed and updated.title != before.title:
                self.reminders.update_enabled_deadline_rule_messages_for_task(
                    updated.id,
                    updated.title,
                    occurred_at,
                    connection=connection,
                )
            if due_changed and updated.status is TaskStatus.PENDING:
                self._create_default_deadline_reminders(updated, occurred_at, connection)
        return updated

    def complete_task(self, reference: str, *, now: datetime | None = None) -> Task:
        occurred_at = self._now(now)
        with self.database.transaction(immediate=True) as connection:
            before = self._resolve_task_in_connection(reference, connection)
            self._require_transition(before, TaskStatus.COMPLETED)
            completed = replace(
                before,
                status=TaskStatus.COMPLETED,
                updated_at=occurred_at,
                completed_at=occurred_at,
                cancelled_at=None,
            )
            self.reminders.cancel_pending_for_task(
                before.id, occurred_at, connection=connection
            )
            self.reminders.disable_deadline_rules_for_task(
                before.id, occurred_at, connection=connection
            )
            self.tasks.update(
                completed, event_type="completed", before=before, connection=connection
            )
        return completed

    def cancel_task(self, reference: str, *, now: datetime | None = None) -> Task:
        occurred_at = self._now(now)
        with self.database.transaction(immediate=True) as connection:
            before = self._resolve_task_in_connection(reference, connection)
            self._require_transition(before, TaskStatus.CANCELLED)
            cancelled = replace(
                before,
                status=TaskStatus.CANCELLED,
                updated_at=occurred_at,
                completed_at=None,
                cancelled_at=occurred_at,
            )
            self.reminders.cancel_pending_for_task(
                before.id, occurred_at, connection=connection
            )
            self.reminders.disable_deadline_rules_for_task(
                before.id, occurred_at, connection=connection
            )
            self.tasks.update(
                cancelled, event_type="cancelled", before=before, connection=connection
            )
        return cancelled

    def reopen_task(self, reference: str, *, now: datetime | None = None) -> Task:
        occurred_at = self._now(now)
        with self.database.transaction(immediate=True) as connection:
            before = self._resolve_task_in_connection(reference, connection)
            self._require_transition(before, TaskStatus.PENDING)
            reopened = replace(
                before,
                status=TaskStatus.PENDING,
                updated_at=occurred_at,
                completed_at=None,
                cancelled_at=None,
            )
            self.reminders.cancel_pending_for_task(
                before.id, occurred_at, connection=connection
            )
            self.reminders.disable_deadline_rules_for_task(
                before.id, occurred_at, connection=connection
            )
            self.tasks.update(
                reopened, event_type="reopened", before=before, connection=connection
            )
            self._create_default_deadline_reminders(reopened, occurred_at, connection)
        return reopened

    def _now(self, value: datetime | None) -> datetime:
        return ensure_utc(value if value is not None else self._clock())

    def _tasks_with_exact_title(self, title: str) -> tuple[Task, ...]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE title = ? ORDER BY id", (title,)
            ).fetchall()
            return tuple(_task_from_row(row) for row in rows)
        finally:
            connection.close()

    @staticmethod
    def _resolve_task_in_connection(
        reference: str, connection: sqlite3.Connection
    ) -> Task:
        if not isinstance(reference, str):
            raise TaskNotFoundError("任务不存在。")
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (reference,)
        ).fetchone()
        if row is not None:
            return _task_from_row(row)

        rows = connection.execute(
            "SELECT * FROM tasks WHERE title = ? ORDER BY id", (reference,)
        ).fetchall()
        if not rows:
            raise TaskNotFoundError("任务不存在。")
        if len(rows) != 1:
            raise AmbiguousTaskReferenceError(
                tuple(_task_from_row(row) for row in rows)
            )
        return _task_from_row(rows[0])

    def _create_default_deadline_reminders(
        self, task: Task, now: datetime, connection: sqlite3.Connection
    ) -> None:
        if (
            task.status is not TaskStatus.PENDING
            or task.due_at is None
            or task.due_at <= now
        ):
            return
        for offset_seconds in _DEFAULT_DEADLINE_OFFSETS:
            rule = self.reminders.create_deadline_rule(
                task, offset_seconds, now, connection=connection
            )
            scheduled_at = task.due_at + timedelta(seconds=offset_seconds)
            if scheduled_at > now:
                self.reminders.ensure_occurrence(
                    rule,
                    scheduled_at,
                    now,
                    connection=connection,
                )

    @staticmethod
    def _require_transition(task: Task, target: TaskStatus) -> None:
        allowed = (
            (task.status is TaskStatus.PENDING and target in (TaskStatus.COMPLETED, TaskStatus.CANCELLED))
            or (
                task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
                and target is TaskStatus.PENDING
            )
        )
        if not allowed:
            raise InvalidTaskTransitionError(
                f"不允许从 {task.status.value} 变更为 {target.value}。"
            )


def _coerce_title(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("任务标题必须是字符串。")
    normalized = value.strip()
    if not normalized:
        raise ValueError("任务标题不能为空。")
    return normalized


def _coerce_priority(value: Priority | str) -> Priority:
    if isinstance(value, Priority):
        return value
    if isinstance(value, str):
        return Priority(value)
    raise TypeError("任务优先级必须是 Priority 或字符串。")


def _coerce_planned_date(value: date | str | None | object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise TypeError("计划日期必须是 date 或 YYYY-MM-DD 字符串。")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("计划日期必须是 YYYY-MM-DD 格式。") from exc
    raise TypeError("计划日期必须是 date 或 YYYY-MM-DD 字符串。")


def _coerce_due_at(value: datetime | str | None | object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        return parse_utc(value)
    raise TypeError("截止时间必须是带时区 datetime 或 RFC3339 字符串。")
