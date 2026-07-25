from datetime import timedelta

from app.summaries.snapshot import SummarySnapshotService
from app.tasks.repository import ReminderRepository, TaskRepository
from app.tasks.service import TaskService


def test_snapshot_contains_facts_and_archive_items(task_database, fixed_now) -> None:
    tasks = TaskRepository(task_database)
    service = TaskService(task_database, tasks, ReminderRepository(task_database))
    completed = service.create_task("完成实验报告", now=fixed_now)
    service.complete_task(completed.id, now=fixed_now + timedelta(hours=1))
    service.create_task(
        "逾期任务", due_at=fixed_now - timedelta(hours=1), allow_past_due=True, now=fixed_now
    )

    snapshot = SummarySnapshotService(task_database, tasks).build(now=fixed_now + timedelta(days=1))

    assert snapshot.iso_year == 2026
    assert snapshot.iso_week == 30
    assert snapshot.stats.created_count == 2
    assert snapshot.stats.completed_count == 1
    assert snapshot.stats.overdue_count == 1
    assert all(item.task_id and item.updated_at for item in snapshot.archive_items)
    assert snapshot.sha256 == snapshot.recalculate_sha256()
