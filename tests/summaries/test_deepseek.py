from __future__ import annotations

import json

from app.summaries.models import ArchiveItem, SnapshotStats, SnapshotTask, WeeklySnapshot
from app.summaries.providers.deepseek import DeepSeekSummaryProvider
from tests.fakes import FakeHttpTransport


def test_deepseek_provider_requests_json_without_secrets() -> None:
    snapshot = WeeklySnapshot(
        iso_year=2026,
        iso_week=30,
        week_start="2026-07-20",
        generated_at="2026-07-25T04:00:00Z",
        created=(),
        completed=(SnapshotTask("task-12345678", "虚构项目", "completed", "normal", None, None, "2026-07-25T04:00:00Z"),),
        cancelled=(), changed=(), ongoing=(), overdue=(),
        archive_items=(ArchiveItem("task-12345678", "虚构项目", "completed", None, None, "2026-07-25T04:00:00Z"),),
        stats=SnapshotStats(1, 1, 0, 0, 0, 0),
    )
    fake_http = FakeHttpTransport({"choices": [{"message": {"content": json.dumps({"overview": "本周完成虚构项目", "completed_items": ["虚构项目"], "ongoing_items": [], "overdue_items": [], "next_focus": []})}}]})
    provider = DeepSeekSummaryProvider(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        api_key="secret-key",
        transport=fake_http,
    )

    result = provider.generate(snapshot)

    payload = fake_http.requests[0].json
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["model"] == "deepseek-v4-pro"
    assert "secret-key" not in json.dumps(payload, ensure_ascii=False)
    assert result.overview
