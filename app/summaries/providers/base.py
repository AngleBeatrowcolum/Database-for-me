from __future__ import annotations

from typing import Protocol

from app.summaries.models import StructuredSummary, WeeklySnapshot


class SummaryProvider(Protocol):
    def generate(self, snapshot: WeeklySnapshot) -> StructuredSummary: ...
