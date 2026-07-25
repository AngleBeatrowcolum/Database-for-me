"""周总结的不可变事实模型和规范化哈希。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from app.tasks.models import ensure_utc, to_utc_text


@dataclass(frozen=True)
class SnapshotTask:
    task_id: str
    title: str
    status: str
    priority: str
    planned_date: str | None
    due_at: str | None
    updated_at: str


@dataclass(frozen=True)
class ArchiveItem:
    task_id: str
    title: str
    status: str
    planned_date: str | None
    due_at: str | None
    updated_at: str


@dataclass(frozen=True)
class SnapshotStats:
    created_count: int
    completed_count: int
    cancelled_count: int
    changed_count: int
    ongoing_count: int
    overdue_count: int
    reminder_sent_count: int = 0
    reminder_failed_count: int = 0
    reminder_skipped_count: int = 0


@dataclass(frozen=True)
class WeeklySnapshot:
    iso_year: int
    iso_week: int
    week_start: str
    generated_at: str
    created: tuple[SnapshotTask, ...]
    completed: tuple[SnapshotTask, ...]
    cancelled: tuple[SnapshotTask, ...]
    changed: tuple[SnapshotTask, ...]
    ongoing: tuple[SnapshotTask, ...]
    overdue: tuple[SnapshotTask, ...]
    archive_items: tuple[ArchiveItem, ...]
    stats: SnapshotStats
    sha256: str = ""

    def __post_init__(self) -> None:
        if not self.sha256:
            object.__setattr__(self, "sha256", self.recalculate_sha256())

    def canonical_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("sha256", None)
        return payload

    def recalculate_sha256(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_dict(self) -> dict[str, object]:
        payload = self.canonical_payload()
        payload["sha256"] = self.sha256
        return payload


@dataclass(frozen=True)
class StructuredSummary:
    overview: str
    completed_items: tuple[str, ...] = ()
    ongoing_items: tuple[str, ...] = ()
    overdue_items: tuple[str, ...] = ()
    next_focus: tuple[str, ...] = ()


def snapshot_task_from_task(task) -> SnapshotTask:
    return SnapshotTask(
        task_id=task.id,
        title=task.title,
        status=task.status.value,
        priority=task.priority.value,
        planned_date=task.planned_date.isoformat() if task.planned_date else None,
        due_at=to_utc_text(task.due_at) if task.due_at else None,
        updated_at=to_utc_text(task.updated_at),
    )


def archive_item_from_task(task) -> ArchiveItem:
    snapshot_task = snapshot_task_from_task(task)
    return ArchiveItem(
        task_id=snapshot_task.task_id,
        title=snapshot_task.title,
        status=snapshot_task.status,
        planned_date=snapshot_task.planned_date,
        due_at=snapshot_task.due_at,
        updated_at=snapshot_task.updated_at,
    )
