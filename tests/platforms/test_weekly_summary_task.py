from pathlib import Path

from app.platforms.weekly_summary_task import build_weekly_summary_task_xml


def test_weekly_task_xml_is_battery_safe(tmp_path: Path) -> None:
    xml = build_weekly_summary_task_xml(python_exe=tmp_path / "python.exe", worker_path=tmp_path / "weekly_summary_worker.py", base_dir=tmp_path)
    assert "<Sunday" in xml
    assert "20:00:00" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<WakeToRun>false</WakeToRun>" in xml
