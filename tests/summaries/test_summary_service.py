from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.summaries.service import SummaryService
from app.tasks.backup import DatabaseBackupService
from app.tasks.errors import ConfirmationRequired
from app.tasks.repository import ReminderRepository, TaskRepository
from app.tasks.service import TaskService


def test_cleanup_requires_published_unchanged_archive(task_database, fixed_now, tmp_path: Path) -> None:
    tasks = TaskRepository(task_database)
    task_service = TaskService(task_database, tasks, ReminderRepository(task_database))
    published = task_service.create_task("已归档的虚构任务", now=fixed_now - timedelta(days=20))
    published = task_service.complete_task(published.id, now=fixed_now - timedelta(days=19))
    changed = task_service.create_task("后续会修改的虚构任务", now=fixed_now - timedelta(days=20))
    changed = task_service.complete_task(changed.id, now=fixed_now - timedelta(days=19))
    unarchived = task_service.create_task("未归档虚构任务", now=fixed_now - timedelta(days=20))
    unarchived = task_service.complete_task(unarchived.id, now=fixed_now - timedelta(days=19))
    service = SummaryService(task_database, tasks, draft_dir=tmp_path / "drafts", snapshot_dir=tmp_path / "snapshots", backup_service=DatabaseBackupService(task_database, tmp_path / "backups"))
    service.create_published_fixture((published, changed), now=fixed_now - timedelta(days=18))
    task_service.update_task(changed.id, title="已修改的虚构任务", now=fixed_now - timedelta(days=17))

    deleted = service.cleanup(now=fixed_now)

    assert deleted.task_ids == (published.id,)
    assert tasks.get(changed.id) is not None
    assert tasks.get(unarchived.id) is not None


def test_publish_requires_explicit_confirmation(task_database, fixed_now, tmp_path: Path) -> None:
    service = SummaryService(task_database, TaskRepository(task_database), draft_dir=tmp_path / "drafts", snapshot_dir=tmp_path / "snapshots")
    run = service.generate(now=fixed_now)

    with pytest.raises(ConfirmationRequired):
        service.publish(run.id, confirmed=False)
