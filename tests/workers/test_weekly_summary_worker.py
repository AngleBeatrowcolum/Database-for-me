from datetime import datetime, timezone
from pathlib import Path

from app.workers.weekly_summary_worker import run_worker


def test_weekly_worker_catches_up_after_sunday(tmp_path: Path) -> None:
    result = run_worker(tmp_path, now=datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc))
    repeated = run_worker(tmp_path, now=datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc))

    assert result.status == "awaiting_approval"
    assert result.run_id == repeated.run_id
