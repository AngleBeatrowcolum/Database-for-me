from datetime import datetime, timezone
from types import SimpleNamespace

from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.qq_mail import QQMailDeliveryError
from app.tasks.models import DeliveryChannel


class FakeScheduler:
    def __init__(self, deliveries: list[object]) -> None:
        self.deliveries = deliveries
        self.sent: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str, str, datetime | None]] = []

    def claim_due(self, channel: DeliveryChannel, *, limit: int) -> list[object]:
        assert channel is DeliveryChannel.EMAIL
        assert limit == 20
        return self.deliveries

    def mark_delivery_sent(self, delivery_id: str, claim_token: str) -> bool:
        self.sent.append((delivery_id, claim_token))
        return True

    def mark_delivery_failed(
        self,
        delivery_id: str,
        claim_token: str,
        error_code: str,
        *,
        next_attempt_at: datetime | None,
    ) -> bool:
        self.failed.append((delivery_id, claim_token, error_code, next_attempt_at))
        return True


def test_dispatcher_sends_and_retries_temporary_email_failure() -> None:
    delivery = SimpleNamespace(
        delivery_id="delivery-1",
        claim_token="claim-1",
        attempt_count=1,
        display_text="任务提醒正文",
    )
    scheduler = FakeScheduler([delivery])

    class TemporaryMailer:
        def send(self, **_kwargs) -> None:
            raise QQMailDeliveryError("temporary", "SMTP 服务暂时不可用。")

    now = datetime(2026, 7, 25, 4, tzinfo=timezone.utc)
    result = NotificationDispatcher(scheduler, TemporaryMailer(), clock=lambda: now).dispatch_email()

    assert result == {"sent": 0, "failed": 1}
    assert scheduler.sent == []
    assert scheduler.failed == [("delivery-1", "claim-1", "temporary", datetime(2026, 7, 25, 4, 10, tzinfo=timezone.utc))]


def test_dispatcher_marks_only_email_delivery_sent() -> None:
    delivery = SimpleNamespace(
        delivery_id="delivery-2",
        claim_token="claim-2",
        attempt_count=1,
        display_text="任务提醒正文",
    )
    scheduler = FakeScheduler([delivery])

    class SuccessfulMailer:
        def send(self, **_kwargs) -> None:
            return None

    result = NotificationDispatcher(scheduler, SuccessfulMailer()).dispatch_email()

    assert result == {"sent": 1, "failed": 0}
    assert scheduler.sent == [("delivery-2", "claim-2")]
    assert scheduler.failed == []
