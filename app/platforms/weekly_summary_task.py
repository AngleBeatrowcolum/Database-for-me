"""Windows 周总结计划任务：周日 20:00 和登录补偿。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


TASK_NAME = r"Sakura\WeeklySummaryWorker"


def build_weekly_summary_task_xml(*, python_exe: Path, worker_path: Path, base_dir: Path) -> str:
    command, arguments = escape(str(Path(python_exe))), escape(f'"{Path(worker_path)}" --base-dir "{Path(base_dir)}"')
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers>
  <CalendarTrigger><StartBoundary>2026-01-04T20:00:00</StartBoundary><Enabled>true</Enabled><ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><Sunday /></DaysOfWeek></ScheduleByWeek></CalendarTrigger>
  <LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT5M</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments></Exec></Actions></Task>'''


def register_weekly_summary_task(xml_path: Path) -> None:
    subprocess.run(["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"], check=True, capture_output=True, text=True)
