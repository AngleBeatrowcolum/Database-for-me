from datetime import datetime, timezone
from pathlib import Path

from app.storage.paths import StoragePaths
from app.tasks.models import Priority, Task, TaskStatus, parse_utc, to_utc_text
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
