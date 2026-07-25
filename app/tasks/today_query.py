"""今日待办的不可变查询与展示模型。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.tasks.models import Priority, Task, ensure_utc, local_date_for_utc
from app.tasks.repository import TaskRepository


_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_PRIORITY_ORDER = {Priority.HIGH: 0, Priority.NORMAL: 1, Priority.LOW: 2}


@dataclass(frozen=True)
class TodaySummary:
    total: int
    high_priority: int
    overdue: int


TodayQuerySummary = TodaySummary


@dataclass(frozen=True)
class TodayQueryResult:
    overdue: tuple[Task, ...]
    due_today: tuple[Task, ...]
    planned_today: tuple[Task, ...]
    summary: TodaySummary

    @property
    def overdue_tasks(self) -> tuple[Task, ...]:
        return self.overdue

    @property
    def due_today_tasks(self) -> tuple[Task, ...]:
        return self.due_today

    @property
    def planned_today_tasks(self) -> tuple[Task, ...]:
        return self.planned_today

    def display_text(self) -> str:
        if self.summary.total == 0:
            return "今天没有待办任务。"

        lines: list[str] = []
        for heading, tasks in (
            ("逾期", self.overdue),
            ("今日截止", self.due_today),
            ("今日计划", self.planned_today),
        ):
            if not tasks:
                continue
            lines.append(f"{heading}：")
            lines.extend(f"- {_display_task(task)}" for task in tasks)
        return "\n".join(lines)

    def speech_text(self) -> str:
        if self.summary.total == 0:
            return "今天没有待办任务。"

        text = (
            f"今天共有{self.summary.total}项待办，其中{self.summary.overdue}项已逾期，"
            f"{self.summary.high_priority}项高优先级。"
        )
        nearest = _nearest_due_task(self)
        if nearest is None:
            return text
        local_due = _as_shanghai(nearest.due_at)
        return (
            f"{text}最近截止的是“{nearest.title}”，"
            f"截止于{local_due:%Y年%m月%d日%H:%M}。"
        )


class TodayQueryService:
    """从待办任务仓储构造今日读模型。"""

    def __init__(self, tasks: TaskRepository | object) -> None:
        self.tasks = tasks
        task_service_list = getattr(tasks, "list_pending_tasks", None)
        self._list_pending: Callable[[], tuple[Task, ...] | list[Task]] = (
            task_service_list if callable(task_service_list) else tasks.list_pending
        )

    def query(self, now: datetime) -> TodayQueryResult:
        utc_now = ensure_utc(now)
        today = _local_date(utc_now)
        overdue: list[Task] = []
        due_today: list[Task] = []
        planned_today: list[Task] = []

        for task in self._list_pending():
            if task.due_at is not None and task.due_at < utc_now:
                overdue.append(task)
            elif (
                task.due_at is not None
                and _local_date(task.due_at) == today
            ):
                due_today.append(task)
            elif task.planned_date == today:
                planned_today.append(task)

        grouped = tuple(
            tuple(sorted(group, key=_task_sort_key))
            for group in (overdue, due_today, planned_today)
        )
        all_tasks = (*grouped[0], *grouped[1], *grouped[2])
        return TodayQueryResult(
            overdue=grouped[0],
            due_today=grouped[1],
            planned_today=grouped[2],
            summary=TodaySummary(
                total=len(all_tasks),
                high_priority=sum(task.priority is Priority.HIGH for task in all_tasks),
                overdue=len(grouped[0]),
            ),
        )


def _task_sort_key(task: Task) -> tuple[int, bool, datetime, datetime, str]:
    return (
        _PRIORITY_ORDER[task.priority],
        task.due_at is None,
        task.due_at if task.due_at is not None else task.created_at,
        task.created_at,
        task.id,
    )


def _display_task(task: Task) -> str:
    priority = {Priority.HIGH: "高", Priority.NORMAL: "普通", Priority.LOW: "低"}[task.priority]
    if task.due_at is not None:
        local_due = _as_shanghai(task.due_at)
        return f"[{priority}] {task.title}（截止 {local_due:%Y-%m-%d %H:%M}）"
    if task.planned_date is not None:
        return f"[{priority}] {task.title}（计划 {task.planned_date.isoformat()}）"
    return f"[{priority}] {task.title}"


def _nearest_due_task(result: TodayQueryResult) -> Task | None:
    due_tasks = [
        task
        for task in (*result.overdue, *result.due_today, *result.planned_today)
        if task.due_at is not None
    ]
    if not due_tasks:
        return None
    return min(due_tasks, key=lambda task: (task.due_at, task.created_at, task.id))


def _local_date(value: datetime):
    try:
        return local_date_for_utc(value, _SHANGHAI_TIMEZONE)
    except ZoneInfoNotFoundError:
        return (ensure_utc(value) + timedelta(hours=8)).date()


def _as_shanghai(value: datetime) -> datetime:
    try:
        return ensure_utc(value).astimezone(ZoneInfo(_SHANGHAI_TIMEZONE))
    except ZoneInfoNotFoundError:
        return ensure_utc(value).astimezone(timezone(timedelta(hours=8), "Asia/Shanghai"))
