from __future__ import annotations

from app.summaries.models import ArchiveItem, SnapshotStats, StructuredSummary, WeeklySnapshot
from app.summaries.renderer import SummaryRenderer


def test_renderer_appends_authoritative_archive_manifest() -> None:
    snapshot = WeeklySnapshot(
        iso_year=2026,
        iso_week=30,
        week_start="2026-07-20",
        generated_at="2026-07-25T04:00:00Z",
        created=(), completed=(), cancelled=(), changed=(), ongoing=(), overdue=(),
        archive_items=(ArchiveItem("task-12345678", "虚构 | 项目\n计划", "completed", None, None, "2026-07-25T04:00:00Z"),),
        stats=SnapshotStats(1, 1, 0, 0, 0, 0),
    )
    markdown = SummaryRenderer().render(snapshot, StructuredSummary("本周完成一项虚构工作。"))

    assert "## 系统归档清单" in markdown
    assert snapshot.archive_items[0].task_id[:8] in markdown
    assert "虚构 \\| 项目 计划" in markdown
    assert f"snapshot-sha256: {snapshot.sha256}" in markdown
