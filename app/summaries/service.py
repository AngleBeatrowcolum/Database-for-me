"""周总结生成、显式发布确认与两周后安全清理。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from app.integrations.git_publisher import GitPublisher
from app.storage.atomic import atomic_write_text
from app.summaries.models import ArchiveItem, SnapshotStats, WeeklySnapshot
from app.summaries.providers.deepseek import SummaryProviderError
from app.summaries.providers.local_fallback import LocalFallbackProvider
from app.summaries.renderer import SummaryRenderer
from app.summaries.snapshot import SummarySnapshotService
from app.tasks.backup import DatabaseBackupService
from app.tasks.database import TaskDatabase
from app.tasks.errors import ConfirmationRequired
from app.tasks.models import Task, WeeklySummaryRun, WeeklySummaryStatus, ensure_utc, parse_utc, to_utc_text
from app.tasks.repository import TaskRepository


@dataclass(frozen=True)
class CleanupResult:
    task_ids: tuple[str, ...]


class SummaryService:
    def __init__(self, database: TaskDatabase, tasks: TaskRepository, *, draft_dir: Path, snapshot_dir: Path, backup_service: DatabaseBackupService | None = None, publisher: GitPublisher | None = None, renderer: SummaryRenderer | None = None) -> None:
        self.database = database
        self.tasks = tasks
        self.draft_dir = Path(draft_dir)
        self.snapshot_dir = Path(snapshot_dir)
        self.backup_service = backup_service
        self.publisher = publisher
        self.renderer = renderer or SummaryRenderer()
        self.snapshots = SummarySnapshotService(database, tasks)

    def generate(self, *, now: datetime, provider=None) -> WeeklySummaryRun:
        current = ensure_utc(now)
        snapshot = self.snapshots.build(now=current)
        run_id = str(uuid.uuid4())
        draft_path = self.draft_dir / f"{snapshot.iso_year}-W{snapshot.iso_week:02d}.md"
        snapshot_path = self.snapshot_dir / f"{snapshot.iso_year}-W{snapshot.iso_week:02d}.json"
        self._insert_run(run_id, snapshot, current)
        selected = provider or LocalFallbackProvider()
        provider_name = "local"
        try:
            summary = selected.generate(snapshot)
            provider_name = "deepseek" if provider is not None else "local"
        except (SummaryProviderError, OSError, ValueError):
            summary = LocalFallbackProvider().generate(snapshot)
            provider_name = "local_fallback"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(snapshot_path, json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", backup=False)
        self.renderer.write_draft(draft_path, snapshot, summary)
        with self.database.transaction() as connection:
            connection.execute("UPDATE weekly_summary_runs SET status=?, provider=?, snapshot_sha256=?, draft_path=?, generated_at=? WHERE id=?", (WeeklySummaryStatus.AWAITING_APPROVAL.value, provider_name, snapshot.sha256, str(draft_path), to_utc_text(current), run_id))
        return self.get(run_id)

    def publish(self, run_id: str, *, confirmed: bool, now: datetime | None = None) -> WeeklySummaryRun:
        if not confirmed:
            raise ConfirmationRequired("上传个人周总结需要显式确认。")
        if self.publisher is None:
            raise RuntimeError("尚未配置私有周总结 Git 仓库。")
        run = self.get(run_id)
        if run.status is not WeeklySummaryStatus.AWAITING_APPROVAL or not run.draft_path:
            raise ValueError("当前周总结不处于可发布状态。")
        current = ensure_utc(now or datetime.now().astimezone())
        with self.database.transaction() as connection:
            connection.execute("UPDATE weekly_summary_runs SET status=?, approved_at=? WHERE id=?", (WeeklySummaryStatus.PUBLISHING.value, to_utc_text(current), run.id))
        result = self.publisher.publish(Path(run.draft_path), iso_year=run.iso_year, iso_week=run.iso_week)
        self._mark_archived_tasks(run, current)
        with self.database.transaction() as connection:
            connection.execute("UPDATE weekly_summary_runs SET status=?, git_commit_sha=?, published_at=? WHERE id=?", (WeeklySummaryStatus.PUBLISHED.value, result.remote_commit_sha, to_utc_text(current), run.id))
        return self.get(run.id)

    def cleanup(self, *, now: datetime) -> CleanupResult:
        current = ensure_utc(now)
        cutoff = current - timedelta(days=14)
        if self.backup_service is not None:
            self.backup_service.create(reason="pre-cleanup", now=current)
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute("""
                SELECT task.id FROM tasks AS task
                JOIN task_summary_archives AS archive ON archive.task_id=task.id AND archive.task_updated_at=task.updated_at
                JOIN weekly_summary_runs AS run ON run.id=archive.summary_run_id AND run.status='published'
                WHERE (task.status='completed' AND task.completed_at <= ?)
                   OR (task.status='cancelled' AND task.cancelled_at <= ?)
                   OR (task.status='pending' AND task.due_at <= ?)
                ORDER BY task.id
            """, (to_utc_text(cutoff), to_utc_text(cutoff), to_utc_text(cutoff))).fetchall()
            task_ids = tuple(str(row["id"]) for row in rows)
            for task_id in task_ids:
                self.tasks.delete(task_id, connection=connection)
        self.database.integrity_check()
        if self.backup_service is not None:
            self.backup_service.create(reason="post-cleanup", now=current)
        return CleanupResult(task_ids)

    def create_published_fixture(self, tasks: tuple[Task, ...], *, now: datetime) -> WeeklySummaryRun:
        """为离线迁移/测试导入已有已发布清单；只接受当前任务版本。"""
        current = ensure_utc(now)
        run_id = str(uuid.uuid4())
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO weekly_summary_runs (id, iso_year, iso_week, week_start, week_end, status, created_at, published_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (run_id, 2026, 1, "2025-12-29", "2026-01-04", "published", to_utc_text(current), to_utc_text(current)))
            for task in tasks:
                connection.execute("INSERT INTO task_summary_archives (task_id, summary_run_id, task_updated_at, archived_at) VALUES (?, ?, ?, ?)", (task.id, run_id, to_utc_text(task.updated_at), to_utc_text(current)))
        return self.get(run_id)

    def get(self, run_id: str) -> WeeklySummaryRun:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM weekly_summary_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"不存在周总结运行记录：{run_id}")
        return _run_from_row(row)

    def _insert_run(self, run_id: str, snapshot: WeeklySnapshot, now: datetime) -> None:
        week_start = date.fromisoformat(snapshot.week_start)
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO weekly_summary_runs (id, iso_year, iso_week, week_start, week_end, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (run_id, snapshot.iso_year, snapshot.iso_week, week_start.isoformat(), (week_start + timedelta(days=6)).isoformat(), WeeklySummaryStatus.GENERATING.value, to_utc_text(now)))

    def _mark_archived_tasks(self, run: WeeklySummaryRun, now: datetime) -> None:
        snapshot_path = self.snapshot_dir / f"{run.iso_year}-W{run.iso_week:02d}.json"
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        items = payload.get("archive_items", [])
        with self.database.transaction() as connection:
            for item in items:
                task = self.tasks.get(str(item.get("task_id", "")))
                if task is None or to_utc_text(task.updated_at) != item.get("updated_at"):
                    continue
                connection.execute("INSERT OR REPLACE INTO task_summary_archives (task_id, summary_run_id, task_updated_at, archived_at) VALUES (?, ?, ?, ?)", (task.id, run.id, item["updated_at"], to_utc_text(now)))


def _run_from_row(row) -> WeeklySummaryRun:
    return WeeklySummaryRun(id=row["id"], iso_year=row["iso_year"], iso_week=row["iso_week"], week_start=date.fromisoformat(row["week_start"]), week_end=date.fromisoformat(row["week_end"]), status=WeeklySummaryStatus(row["status"]), provider=row["provider"], snapshot_sha256=row["snapshot_sha256"], draft_path=row["draft_path"], git_commit_sha=row["git_commit_sha"], last_error_code=row["last_error_code"], created_at=parse_utc(row["created_at"]), generated_at=parse_utc(row["generated_at"]) if row["generated_at"] else None, approved_at=parse_utc(row["approved_at"]) if row["approved_at"] else None, published_at=parse_utc(row["published_at"]) if row["published_at"] else None, cleaned_at=parse_utc(row["cleaned_at"]) if row["cleaned_at"] else None)
