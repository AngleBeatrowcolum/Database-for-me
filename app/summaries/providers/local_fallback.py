"""永不联网的周总结降级提供者。"""

from __future__ import annotations

from app.summaries.models import StructuredSummary, WeeklySnapshot


class LocalFallbackProvider:
    def generate(self, snapshot: WeeklySnapshot) -> StructuredSummary:
        stats = snapshot.stats
        return StructuredSummary(
            overview=(
                f"本周创建 {stats.created_count} 项，完成 {stats.completed_count} 项，"
                f"当前仍有 {stats.ongoing_count} 项待处理，其中逾期 {stats.overdue_count} 项。"
            ),
            completed_items=tuple(item.title for item in snapshot.completed),
            ongoing_items=tuple(item.title for item in snapshot.ongoing),
            overdue_items=tuple(item.title for item in snapshot.overdue),
            next_focus=tuple(item.title for item in snapshot.overdue or snapshot.ongoing),
        )
