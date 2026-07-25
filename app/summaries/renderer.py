"""把受限的结构化总结与本地事实渲染为可审计 Markdown。"""

from __future__ import annotations

from pathlib import Path

from app.storage.atomic import atomic_write_text
from app.summaries.models import StructuredSummary, WeeklySnapshot


def _escape_cell(value: str | None) -> str:
    return (value or "-").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _items(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 无"


class SummaryRenderer:
    def render(self, snapshot: WeeklySnapshot, summary: StructuredSummary) -> str:
        stats = snapshot.stats
        changed = tuple(item.title for item in snapshot.changed)
        rows = ["| ID | 标题 | 状态 | 计划日期 | 截止时间 | 最后更新 |", "| --- | --- | --- | --- | --- | --- |"]
        rows.extend(
            "| {id} | {title} | {status} | {planned} | {due} | {updated} |".format(id=_escape_cell(item.task_id[:8]), title=_escape_cell(item.title), status=_escape_cell(item.status), planned=_escape_cell(item.planned_date), due=_escape_cell(item.due_at), updated=_escape_cell(item.updated_at))
            for item in snapshot.archive_items
        )
        return "\n".join((
            f"# {snapshot.iso_year} 年第 {snapshot.iso_week} 周个人总结", "", "## 本周概览", summary.overview.strip() or "- 无", "", "## 主要完成事项", _items(summary.completed_items), "", "## 仍在进行的工作", _items(summary.ongoing_items), "", "## 逾期未完成事项", _items(summary.overdue_items), "", "## 计划变更与延期", _items(changed), "", "## 时间与任务统计", f"- 创建：{stats.created_count} 项；完成：{stats.completed_count} 项；取消：{stats.cancelled_count} 项；变更：{stats.changed_count} 项。", f"- 进行中：{stats.ongoing_count} 项；逾期：{stats.overdue_count} 项。", "", "## 下周建议关注", _items(summary.next_focus), "", "## 系统归档清单", *rows, "", f"<!-- snapshot-sha256: {snapshot.sha256} -->", "",
        ))

    def write_draft(self, path: Path, snapshot: WeeklySnapshot, summary: StructuredSummary) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, self.render(snapshot, summary), encoding="utf-8", backup=False)
