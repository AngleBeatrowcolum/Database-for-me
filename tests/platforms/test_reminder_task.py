from pathlib import Path

from app.platforms.reminder_task import build_reminder_task_xml


def test_windows_task_xml_is_battery_safe(tmp_path: Path) -> None:
    xml = build_reminder_task_xml(
        python_exe=tmp_path / "runtime" / "python.exe",
        worker_path=tmp_path / "app" / "workers" / "reminder_worker.py",
        base_dir=tmp_path,
    )

    assert "<Interval>PT10M</Interval>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<WakeToRun>false</WakeToRun>" in xml
    assert "<LogonTrigger>" in xml
    assert "reminder_worker.py" in xml
    assert "<WorkingDirectory>" in xml
    assert str((tmp_path / "runtime" / "python.exe").resolve()) in xml
