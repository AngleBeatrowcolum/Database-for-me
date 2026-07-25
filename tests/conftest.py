from datetime import datetime, timezone
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tasks.database import TaskDatabase


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def task_database(tmp_path: Path) -> TaskDatabase:
    database = TaskDatabase(tmp_path / "tasks.db")
    database.initialize()
    return database
