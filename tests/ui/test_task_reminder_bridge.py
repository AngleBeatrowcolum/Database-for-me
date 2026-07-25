from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.agent.actions import AgentEvent
from app.tasks.models import DeliveryChannel
from app.ui.task_reminder_bridge import TaskReminderBridge


class FakeScheduler:
    def __init__(self, deliveries: list[object]) -> None:
        self.deliveries = deliveries
        self.claim_calls: list[tuple[DeliveryChannel, int]] = []
        self.sent: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str, str, object]] = []

    def claim_due(self, channel: DeliveryChannel, *, limit: int) -> list[object]:
        self.claim_calls.append((channel, limit))
        return list(self.deliveries)

    def mark_delivery_sent(self, delivery_id: str, claim_token: str) -> bool:
        self.sent.append((delivery_id, claim_token))
        return True

    def mark_delivery_failed(
        self,
        delivery_id: str,
        claim_token: str,
        error_code: str,
        *,
        next_attempt_at: object,
    ) -> bool:
        self.failed.append((delivery_id, claim_token, error_code, next_attempt_at))
        return True


def test_bridge_uses_prepared_text_and_marks_only_delivery_sent() -> None:
    delivery = SimpleNamespace(
        delivery_id="delivery-1",
        claim_token="claim-1",
        channel=DeliveryChannel.DESKTOP,
        message="实验报告",
        task_id="task-1",
        scheduled_at=datetime(2026, 7, 25, 4, tzinfo=timezone.utc),
        coalesced_count=2,
        display_text="高优先级：实验报告将在 15:00 截止。",
        speech_text="实验报告将在下午三点截止。",
    )
    scheduler = FakeScheduler([delivery])
    bridge = TaskReminderBridge(scheduler)

    prepared = bridge.poll()

    assert prepared is not None
    assert prepared.event == AgentEvent(
        type="reminder_due",
        payload={
            "delivery_id": "delivery-1",
            "task_id": "task-1",
            "scheduled_at": "2026-07-25T04:00:00+00:00",
        },
    )
    assert prepared.reply.translation == delivery.display_text
    assert prepared.reply.text == delivery.speech_text
    assert scheduler.claim_calls == [(DeliveryChannel.DESKTOP, 1)]

    assert bridge.mark_shown(prepared) is True
    assert scheduler.sent == [("delivery-1", "claim-1")]
    assert scheduler.failed == []


def test_bridge_returns_none_when_no_desktop_delivery_is_due() -> None:
    scheduler = FakeScheduler([])

    assert TaskReminderBridge(scheduler).poll() is None
    assert scheduler.sent == []
