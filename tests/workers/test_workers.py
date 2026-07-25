from pathlib import Path

from app.workers.reminder_worker import run_worker

def test_reminder_worker_does_not_import_qt() -> None:
    source = Path("app/workers/reminder_worker.py").read_text(encoding="utf-8")

    assert "PySide6" not in source
    assert "app.ui" not in source


def test_worker_exits_cleanly_when_email_is_not_enabled(tmp_path: Path) -> None:
    result = run_worker(tmp_path)

    assert result.skipped is True
    assert result.sent == 0
    assert result.failed == 0
