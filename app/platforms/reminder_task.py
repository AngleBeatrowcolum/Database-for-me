"""生成并注册省电的 Windows 任务计划提醒 worker。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


TASK_NAME = r"Sakura\TaskReminderWorker"


def build_reminder_task_xml(
    *, python_exe: Path, worker_path: Path, base_dir: Path
) -> str:
    """构建每十分钟运行一次、允许电池且不唤醒设备的计划任务 XML。"""

    command = escape(str(Path(python_exe)))
    arguments = escape(f'"{Path(worker_path)}" --base-dir "{Path(base_dir)}"')
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT10M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>'''


def write_reminder_task_xml(
    path: Path, *, python_exe: Path, worker_path: Path, base_dir: Path
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_reminder_task_xml(
            python_exe=python_exe, worker_path=worker_path, base_dir=base_dir
        ),
        encoding="utf-16",
    )
    return path


def register_reminder_task(xml_path: Path) -> None:
    """显式注册计划任务；仅由手动配置命令调用。"""

    subprocess.run(
        ["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
        check=True,
        capture_output=True,
        text=True,
    )
