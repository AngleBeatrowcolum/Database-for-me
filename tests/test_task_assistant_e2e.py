from datetime import timedelta
from pathlib import Path

from app.summaries.service import SummaryService
from app.tasks.repository import ReminderRepository, TaskRepository
from app.tasks.service import TaskService


def test_local_task_and_weekly_summary_workflow(task_database, fixed_now, tmp_path: Path) -> None:
    tasks = TaskRepository(task_database)
    task_service = TaskService(task_database, tasks, ReminderRepository(task_database))
    task = task_service.create_task("虚构实验报告", priority="high", planned_date="2026-07-25", due_at=fixed_now + timedelta(days=1), now=fixed_now)

    assert task_service.query_today(now=fixed_now).summary.high_priority == 1
    run = SummaryService(task_database, tasks, draft_dir=tmp_path / "drafts", snapshot_dir=tmp_path / "snapshots").generate(now=fixed_now)

    assert run.status.value == "awaiting_approval"
    assert Path(run.draft_path).is_file()
    assert tasks.get(task.id) is not None
