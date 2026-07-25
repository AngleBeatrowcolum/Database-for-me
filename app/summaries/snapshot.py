"""从任务事件和当前任务状态生成确定性的周总结事实快照。"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.summaries.models import (
    SnapshotStats,
    WeeklySnapshot,
    archive_item_from_task,
    snapshot_task_from_task,
)
from app.tasks.database import TaskDatabase
from app.tasks.models import TaskStatus, ensure_utc, to_utc_text
from app.tasks.repository import TaskRepository


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class SummarySnapshotService:
    def __init__(self, database: TaskDatabase, tasks: TaskRepository) -> None:
        self._database = database
        self._tasks = tasks

    def build(self, *, now: datetime) -> WeeklySnapshot:
        utc_now = ensure_utc(now)
        local_now = utc_now.astimezone(_SHANGHAI)
        week_start_date = local_now.date() - timedelta(days=local_now.isoweekday() - 1)
        week_start = datetime.combine(week_start_date, time.min, tzinfo=_SHANGHAI).astimezone(
            utc_now.tzinfo
        )
        events = self._tasks.list_events_between(week_start, utc_now)
        event_task_ids = {str(event["task_id"]) for event in events}
        tasks_by_id = {
            task_id: task
            for task_id in event_task_ids
            if (task := self._tasks.get(task_id)) is not None
        }
        pending = self._tasks.list_pending()
        tasks_by_id.update({task.id: task for task in pending})

        def items_for(event_type: str):
            ids = {str(event["task_id"]) for event in events if event["event_type"] == event_type}
            return tuple(
                snapshot_task_from_task(tasks_by_id[task_id])
                for task_id in sorted(ids)
                if task_id in tasks_by_id
            )

        created = items_for("created")
        completed = items_for("completed")
        cancelled = items_for("cancelled")
        changed_ids = {
            str(event["task_id"])
            for event in events
            if event["event_type"] in {"updated", "reopened"}
        }
        changed = tuple(
            snapshot_task_from_task(tasks_by_id[task_id])
            for task_id in sorted(changed_ids)
            if task_id in tasks_by_id
        )
        ongoing = tuple(snapshot_task_from_task(task) for task in pending)
        overdue = tuple(
            snapshot_task_from_task(task)
            for task in pending
            if task.due_at is not None and task.due_at < utc_now
        )
        archive_items = tuple(
            archive_item_from_task(tasks_by_id[task_id]) for task_id in sorted(tasks_by_id)
        )
        stats = SnapshotStats(
            created_count=len(created),
            completed_count=len(completed),
            cancelled_count=len(cancelled),
            changed_count=len(changed),
            ongoing_count=len(ongoing),
            overdue_count=len(overdue),
        )
        iso_year, iso_week, _ = local_now.isocalendar()
        return WeeklySnapshot(
            iso_year=iso_year,
            iso_week=iso_week,
            week_start=week_start_date.isoformat(),
            generated_at=to_utc_text(utc_now),
            created=created,
            completed=completed,
            cancelled=cancelled,
            changed=changed,
            ongoing=ongoing,
            overdue=overdue,
            archive_items=archive_items,
            stats=stats,
        )
