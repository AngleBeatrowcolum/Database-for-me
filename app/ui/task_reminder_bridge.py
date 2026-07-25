"""将已领取的桌面提醒转为 Sakura 可直接显示的确定性回复。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.agent.actions import AgentEvent
from app.llm.chat_reply import ChatReply, ChatSegment
from app.tasks.models import DeliveryChannel


class _DesktopReminderScheduler(Protocol):
    def claim_due(
        self, channel: DeliveryChannel, *, limit: int
    ) -> list[object]: ...

    def mark_delivery_sent(self, delivery_id: str, claim_token: str) -> bool: ...

    def mark_delivery_failed(
        self,
        delivery_id: str,
        claim_token: str,
        error_code: str,
        *,
        next_attempt_at: object,
    ) -> bool: ...


@dataclass(frozen=True)
class PreparedDesktopReminder:
    """一条已经领取、可直接进入气泡和 TTS 链路的桌面提醒。"""

    delivery_id: str
    claim_token: str
    event: AgentEvent
    reply: ChatReply


class TaskReminderBridge:
    """不调用模型的桌面提醒桥接层。"""

    def __init__(self, scheduler: _DesktopReminderScheduler) -> None:
        self._scheduler = scheduler

    def poll(self) -> PreparedDesktopReminder | None:
        """领取至多一条到期桌面投递，并保留调度器准备好的文本。"""

        claimed = self._scheduler.claim_due(DeliveryChannel.DESKTOP, limit=1)
        if not claimed:
            return None
        delivery = claimed[0]
        delivery_id = str(getattr(delivery, "delivery_id"))
        claim_token = str(getattr(delivery, "claim_token"))
        task_id = getattr(delivery, "task_id", None)
        scheduled_at = getattr(delivery, "scheduled_at")
        event = AgentEvent(
            type="reminder_due",
            payload={
                "delivery_id": delivery_id,
                "task_id": task_id,
                "scheduled_at": scheduled_at.isoformat(),
            },
        )
        reply = ChatReply(
            [
                ChatSegment(
                    text=str(getattr(delivery, "speech_text")),
                    translation=str(getattr(delivery, "display_text")),
                    tone="提醒",
                )
            ]
        )
        return PreparedDesktopReminder(
            delivery_id=delivery_id,
            claim_token=claim_token,
            event=event,
            reply=reply,
        )

    def mark_shown(self, prepared: PreparedDesktopReminder) -> bool:
        """仅确认该桌面渠道已展示；绝不改变任务完成状态。"""

        return self._scheduler.mark_delivery_sent(
            prepared.delivery_id, prepared.claim_token
        )

    def mark_failed(
        self,
        prepared: PreparedDesktopReminder,
        error_code: str = "desktop_render_failed",
    ) -> bool:
        """渲染失败时只结束当前桌面投递，不影响任务或邮件渠道。"""

        return self._scheduler.mark_delivery_failed(
            prepared.delivery_id,
            prepared.claim_token,
            error_code,
            next_attempt_at=None,
        )
