"""生成提醒实例、合并错过的截止提醒，并原子领取待发送渠道。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from app.tasks.models import (
    DeliveryChannel,
    DeliveryStatus,
    ReminderKind,
    ReminderRule,
    Task,
    TaskStatus,
    ensure_utc,
)
from app.tasks.repository import ClaimedReminderDelivery, ReminderRepository


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_STOCK_WEEKDAYS_MASK = 0b0111110
_STOCK_TIME_OF_DAY = "14:00:00"
_STOCK_GRACE_SECONDS = 30 * 60
_STOCK_MESSAGE = "请检查今天的个人计划；本提醒不提供投资建议。"
_DEFAULT_LIMIT = 20
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PreparedDelivery:
    """已领取且可由桌宠或邮件通道确定性展示的提醒。"""

    delivery_id: str
    claim_token: str
    channel: DeliveryChannel
    message: str
    task_id: str | None
    scheduled_at: datetime
    coalesced_count: int
    display_text: str
    speech_text: str


class ReminderScheduler:
    """任务域的到期调度服务，不发送网络请求也不依赖 UI。"""

    def __init__(
        self,
        reminders: ReminderRepository,
        *,
        clock: Clock = _utc_now,
        lease_seconds: int = 300,
    ) -> None:
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool):
            raise TypeError("租约秒数必须是整数。")
        if lease_seconds <= 0:
            raise ValueError("租约秒数必须大于零。")
        self.reminders = reminders
        self._clock = clock
        self._lease_seconds = lease_seconds

    def ensure_stock_rule(self, *, now: datetime | None = None) -> ReminderRule:
        """确保存在一条工作日 14:00 的个人计划检查提醒。"""

        occurred_at = self._now(now)
        with self.reminders.database.transaction(immediate=True) as connection:
            existing = self.reminders.find_enabled_weekly_rule(
                message=_STOCK_MESSAGE,
                weekdays_mask=_STOCK_WEEKDAYS_MASK,
                time_of_day=_STOCK_TIME_OF_DAY,
                grace_seconds=_STOCK_GRACE_SECONDS,
                timezone_name="Asia/Shanghai",
                connection=connection,
            )
            if existing is not None:
                return existing
            return self.reminders.create_weekly_rule(
                _STOCK_MESSAGE,
                weekdays_mask=_STOCK_WEEKDAYS_MASK,
                time_of_day=_STOCK_TIME_OF_DAY,
                grace_seconds=_STOCK_GRACE_SECONDS,
                now=occurred_at,
                connection=connection,
            )

    def generate_occurrences(self, *, now: datetime | None = None) -> None:
        """按当前时点补齐启用规则应有的实例；重复调用保持幂等。"""

        occurred_at = self._now(now)
        with self.reminders.database.transaction(immediate=True) as connection:
            self._generate_occurrences(connection, occurred_at)

    def claim_due(
        self,
        channel: DeliveryChannel | str,
        *,
        now: datetime | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[PreparedDelivery]:
        """生成、清理和合并后，在一个短事务内领取指定渠道的到期投递。"""

        delivery_channel = _coerce_channel(channel)
        _validate_limit(limit)
        occurred_at = self._now(now)
        with self.reminders.database.transaction(immediate=True) as connection:
            self._generate_occurrences(connection, occurred_at)
            self.reminders.recover_expired_delivery_leases(
                occurred_at,
                connection=connection,
                lease_seconds=self._lease_seconds,
            )
            self.reminders.expire_overdue_weekly_occurrences(
                occurred_at, connection=connection
            )
            self.reminders.coalesce_pending_deadline_deliveries(
                delivery_channel, occurred_at, connection=connection
            )
            claimed = self.reminders.claim_due_deliveries(
                delivery_channel,
                occurred_at,
                limit,
                lease_seconds=self._lease_seconds,
                connection=connection,
            )
            contexts = self.reminders.claimed_delivery_contexts(
                claimed, connection=connection
            )
            return [
                self._prepare_delivery(
                    context,
                    now=occurred_at,
                    coalesced_count=self.reminders.coalesced_count_for_delivery(
                        context.delivery.id, connection=connection
                    ),
                )
                for context in contexts
            ]

    def mark_delivery_sent(
        self,
        delivery_id: str,
        claim_token: str,
        *,
        sent_at: datetime | None = None,
    ) -> bool:
        """仅将持有有效租约的一个渠道标记成功，不改变任务状态。"""

        return self.reminders.mark_delivery_sent(
            delivery_id, claim_token, self._now(sent_at)
        )

    def mark_delivery_failed(
        self,
        delivery_id: str,
        claim_token: str,
        error_code: str,
        *,
        next_attempt_at: datetime | None,
    ) -> bool:
        """记录单渠道失败；重试时间由调用方的通道策略决定。"""

        retry_at = ensure_utc(next_attempt_at) if next_attempt_at is not None else None
        return self.reminders.mark_delivery_failed(
            delivery_id, claim_token, error_code, retry_at
        )

    def mark_delivery_skipped(
        self, delivery_id: str, claim_token: str, reason: str
    ) -> bool:
        """标记持有租约的一个渠道为跳过。"""

        return self.reminders.mark_delivery_skipped(delivery_id, claim_token, reason)

    def skipped_reason_for(self, local_day: date | str) -> str | None:
        """返回上海本地日期内最近一条周期提醒的跳过原因。"""

        if isinstance(local_day, str):
            try:
                local_day = date.fromisoformat(local_day)
            except ValueError as exc:
                raise ValueError("日期必须是 YYYY-MM-DD 格式。") from exc
        if not isinstance(local_day, date) or isinstance(local_day, datetime):
            raise TypeError("日期必须是 date 或 YYYY-MM-DD 字符串。")
        return self.reminders.weekly_skip_reason_for_local_date(local_day)

    def _generate_occurrences(self, connection, now: datetime) -> None:
        for rule, task in self.reminders.list_enabled_rules_with_tasks(connection):
            if rule.kind is ReminderKind.DEADLINE_OFFSET:
                self._ensure_deadline_occurrence(rule, task, now, connection)
            elif rule.kind is ReminderKind.WEEKLY:
                self._ensure_weekly_occurrence(rule, now, connection)
            elif rule.kind is ReminderKind.ONE_TIME:
                # 一次性规则在创建事务中已生成唯一实例，绝不在此重建。
                continue

    def _ensure_deadline_occurrence(
        self, rule: ReminderRule, task: Task | None, now: datetime, connection
    ) -> None:
        if (
            task is None
            or task.status is not TaskStatus.PENDING
            or task.due_at is None
            or rule.offset_seconds is None
        ):
            return
        self.reminders.ensure_occurrence(
            rule,
            task.due_at + timedelta(seconds=rule.offset_seconds),
            now,
            connection=connection,
        )

    def _ensure_weekly_occurrence(
        self, rule: ReminderRule, now: datetime, connection
    ) -> None:
        if (
            rule.weekdays_mask is None
            or rule.time_of_day is None
            or rule.grace_seconds is None
        ):
            return
        rule_timezone = ZoneInfo(rule.timezone)
        local_now = now.astimezone(rule_timezone)
        if not _matches_weekday(rule.weekdays_mask, local_now.isoweekday()):
            return
        try:
            local_time = time.fromisoformat(rule.time_of_day)
        except ValueError as exc:
            raise ValueError(f"周期提醒时间格式无效：{rule.time_of_day}") from exc
        scheduled_local = datetime.combine(
            local_now.date(), local_time, tzinfo=rule_timezone
        )
        self.reminders.ensure_occurrence(
            rule, scheduled_local.astimezone(timezone.utc), now, connection=connection
        )

    @staticmethod
    def _prepare_delivery(
        context: ClaimedReminderDelivery,
        *,
        now: datetime,
        coalesced_count: int,
    ) -> PreparedDelivery:
        delivery = context.delivery
        if delivery.status is not DeliveryStatus.SENDING or not delivery.claim_token:
            raise RuntimeError("只能准备已领取且持有租约的提醒投递。")
        display_text, speech_text = _render_prepared_text(
            context, now=now, coalesced_count=coalesced_count
        )
        return PreparedDelivery(
            delivery_id=delivery.id,
            claim_token=delivery.claim_token,
            channel=delivery.channel,
            message=context.rule.message,
            task_id=context.occurrence.task_id,
            scheduled_at=context.occurrence.scheduled_at,
            coalesced_count=coalesced_count,
            display_text=display_text,
            speech_text=speech_text,
        )

    def _now(self, value: datetime | None) -> datetime:
        return ensure_utc(value if value is not None else self._clock())


def _coerce_channel(value: DeliveryChannel | str) -> DeliveryChannel:
    if isinstance(value, DeliveryChannel):
        return value
    if isinstance(value, str):
        try:
            return DeliveryChannel(value)
        except ValueError as exc:
            raise ValueError("通知渠道仅支持 desktop 或 email。") from exc
    raise TypeError("通知渠道必须是 DeliveryChannel 或字符串。")


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("领取数量必须是整数。")
    if limit < 0:
        raise ValueError("领取数量不能为负数。")


def _matches_weekday(mask: int, iso_weekday: int) -> bool:
    return bool(mask & (1 << iso_weekday))


def _render_prepared_text(
    context: ClaimedReminderDelivery, *, now: datetime, coalesced_count: int
) -> tuple[str, str]:
    scheduled_local = context.occurrence.scheduled_at.astimezone(_SHANGHAI)
    scheduled_text = scheduled_local.strftime("%Y-%m-%d %H:%M")
    task_is_overdue = bool(
        context.task is not None
        and context.task.due_at is not None
        and context.task.due_at < now
    )
    if context.rule.kind is ReminderKind.DEADLINE_OFFSET:
        state = "任务已逾期" if task_is_overdue else "任务尚未逾期"
    else:
        state = "提醒时间已到"

    lines = [
        f"任务提醒：{context.rule.message}",
        f"原计划提醒时间：{scheduled_text}",
        f"当前状态：{state}。",
    ]
    if coalesced_count > 1:
        lines.append(f"本次已合并 {coalesced_count} 个错过的截止提醒。")
    display_text = "\n".join(lines)
    speech_text = f"提醒：{context.rule.message}。原定 {scheduled_local.strftime('%H:%M')}。{state}。"
    if coalesced_count > 1:
        speech_text = f"{speech_text}已合并 {coalesced_count} 个提醒。"
    return display_text, speech_text
