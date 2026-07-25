"""计划任务调用的短生命周期周总结草稿生成器；绝不自行发布。"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.storage.paths import StoragePaths
from app.summaries.service import SummaryService
from app.tasks.database import TaskDatabase
from app.tasks.repository import TaskRepository
from app.tasks.settings import TaskAssistantSettings

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class WeeklyWorkerResult:
    run_id: str | None
    status: str
    skipped: bool = False


def run_worker(base_dir: Path, *, now: datetime | None = None) -> WeeklyWorkerResult:
    paths = StoragePaths(base_dir)
    settings = TaskAssistantSettings.load(paths.task_assistant_config())
    if not settings.summary_enabled:
        return WeeklyWorkerResult(None, "disabled", True)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local = current.astimezone(_SHANGHAI)
    target_local = local if local.isoweekday() == 7 and local.time() >= time(20) else local - timedelta(days=local.isoweekday())
    target_iso = target_local.isocalendar()
    database = TaskDatabase(paths.tasks_database()); database.initialize()
    service = SummaryService(database, TaskRepository(database), draft_dir=paths.weekly_summary_drafts_dir, snapshot_dir=paths.weekly_summary_snapshots_dir)
    existing = service.get_by_week(target_iso.year, target_iso.week)
    if existing is not None:
        return WeeklyWorkerResult(existing.id, existing.status.value)
    week_end = target_local.date() + timedelta(days=(7 - target_local.isoweekday()))
    snapshot_now = datetime.combine(week_end, time.max, tzinfo=_SHANGHAI).astimezone(timezone.utc)
    run = service.generate(now=current, snapshot_now=snapshot_now)
    return WeeklyWorkerResult(run.id, run.status.value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Sakura 周总结草稿。")
    parser.add_argument("--base-dir", type=Path, default=_PROJECT_ROOT)
    arguments = parser.parse_args(argv)
    run_worker(arguments.base_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
