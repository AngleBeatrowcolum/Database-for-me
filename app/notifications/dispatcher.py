"""按渠道领取并投递任务提醒。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from app.notifications.qq_mail import QQMailDeliveryError, QQMailer
from app.tasks.models import DeliveryChannel


Clock = Callable[[], datetime]
_RETRY_DELAYS_SECONDS = (10 * 60, 30 * 60, 60 * 60)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _EmailScheduler(Protocol):
    def claim_due(self, channel: DeliveryChannel, *, limit: int) -> list[object]: ...

    def mark_delivery_sent(self, delivery_id: str, claim_token: str) -> bool: ...

    def mark_delivery_failed(
        self,
        delivery_id: str,
        claim_token: str,
        error_code: str,
        *,
        next_attempt_at: datetime | None,
    ) -> bool: ...


class NotificationDispatcher:
    """邮件渠道的短生命周期分发器；不加载 UI 或模型。"""

    def __init__(
        self,
        scheduler: _EmailScheduler,
        mailer: QQMailer,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._scheduler = scheduler
        self._mailer = mailer
        self._clock = clock

    def dispatch_email(self, *, limit: int = 20) -> dict[str, int]:
        sent = 0
        failed = 0
        for delivery in self._scheduler.claim_due(DeliveryChannel.EMAIL, limit=limit):
            try:
                self._mailer.send(subject="Sakura 任务提醒", body=delivery.display_text)
            except QQMailDeliveryError as exc:
                failed += 1
                next_attempt = _retry_time(delivery, exc.code, self._clock())
                self._scheduler.mark_delivery_failed(
                    delivery.delivery_id,
                    delivery.claim_token,
                    exc.code,
                    next_attempt_at=next_attempt,
                )
            else:
                sent += 1
                self._scheduler.mark_delivery_sent(
                    delivery.delivery_id, delivery.claim_token
                )
        return {"sent": sent, "failed": failed}


def _retry_time(delivery: object, code: str, now: datetime) -> datetime | None:
    if code != "temporary":
        return None
    attempt_count = getattr(delivery, "attempt_count", 1)
    if not isinstance(attempt_count, int) or attempt_count < 1 or attempt_count > 3:
        return None
    return now + timedelta(seconds=_RETRY_DELAYS_SECONDS[attempt_count - 1])
