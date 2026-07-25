from __future__ import annotations

from pathlib import Path

import app.core.bootstrap as bootstrap
import pytest
from app.agent.reminders import ReminderStore
from app.core.app_context import AppContext, StorageServices
from app.tasks.service import TaskService


def test_task_storage_bootstrap_initializes_sqlite_and_runs_legacy_migration(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[object, Path]] = []

    class RecordingMigrator:
        def __init__(self, database, legacy_data_dir: Path) -> None:
            calls.append((database, legacy_data_dir))

        def run(self) -> None:
            return None

    monkeypatch.setattr(bootstrap, "LegacyJsonMigrator", RecordingMigrator)

    services = bootstrap.create_task_storage_services(tmp_path)

    assert isinstance(services.task_service, TaskService)
    assert services.task_service.database.path == tmp_path / "data" / "tasks.db"
    assert services.task_service.database.path.exists()
    assert calls == [(services.task_service.database, tmp_path / "data")]


def test_storage_services_and_app_context_expose_task_services(task_database, fixed_now) -> None:
    from app.agent.task_tools import SQLiteOneTimeReminderAdapter
    from app.tasks.repository import ReminderRepository, TaskRepository

    task_service = TaskService(
        task_database,
        TaskRepository(task_database),
        ReminderRepository(task_database),
        clock=lambda: fixed_now,
    )
    reminder_scheduler = SQLiteOneTimeReminderAdapter(
        ReminderRepository(task_database), clock=lambda: fixed_now
    )
    with pytest.deprecated_call(match="ReminderStore"):
        reminder_store = ReminderStore(reminder_scheduler)
    storage = StorageServices(
        memory_store=object(),
        task_service=task_service,
        reminder_scheduler=reminder_scheduler,
        reminder_store=reminder_store,
        history_store=object(),
        visual_observation_store=object(),
        runtime_event_log=object(),
    )
    context = object.__new__(AppContext)
    object.__setattr__(context, "storage", storage)

    assert context.task_service is task_service
    assert context.reminder_scheduler is reminder_scheduler
