"""任务助手的不可变领域模型。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo


class TaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class ReminderKind(str, Enum):
    DEADLINE_OFFSET = "deadline_offset"
    ONE_TIME = "one_time"
    WEEKLY = "weekly"


class DeliveryChannel(str, Enum):
    DESKTOP = "desktop"
    EMAIL = "email"


class ReminderOccurrenceStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


# 保留首版公开名称，避免后续消费者因规范名称调整而失效。
NotificationDeliveryStatus = DeliveryStatus


class WeeklySummaryStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    AWAITING_APPROVAL = "awaiting_approval"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    CLEANED = "cleaned"
    FAILED = "failed"


def ensure_utc(value: datetime) -> datetime:
    """验证 datetime 带时区并归一化为 UTC。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区信息。")
    return value.astimezone(timezone.utc)


def to_utc_text(value: datetime) -> str:
    """将带时区时间转为可持久化的 UTC ISO 文本。"""

    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """解析 ISO 时间文本并归一化为 UTC。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间必须是非空 ISO 字符串。")

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"时间必须是 ISO 格式：{value}") from exc
    return ensure_utc(parsed)


def local_date_for_utc(value: datetime, timezone_name: str) -> date:
    """返回某个 IANA 时区中对应 UTC 时间的本地日期。"""

    return ensure_utc(value).astimezone(ZoneInfo(timezone_name)).date()


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    details: str
    status: TaskStatus
    priority: Priority
    planned_date: date | None
    due_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, TaskStatus):
            raise TypeError("任务状态必须是 TaskStatus。")
        if not isinstance(self.priority, Priority):
            raise TypeError("任务优先级必须是 Priority。")
        for field_name in (
            "due_at",
            "created_at",
            "updated_at",
            "completed_at",
            "cancelled_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value))

    @classmethod
    def new(
        cls,
        title: str,
        *,
        now: datetime,
        details: str = "",
        priority: Priority = Priority.NORMAL,
        planned_date: date | None = None,
        due_at: datetime | None = None,
    ) -> "Task":
        if not isinstance(title, str):
            raise TypeError("任务标题必须是字符串。")
        if not isinstance(details, str):
            raise TypeError("任务详情必须是字符串。")
        if not isinstance(priority, Priority):
            raise TypeError("任务优先级必须是 Priority。")
        normalized = title.strip()
        if not normalized:
            raise ValueError("任务标题不能为空。")
        utc_now = ensure_utc(now)
        return cls(
            id=str(uuid.uuid4()),
            title=normalized,
            details=details.strip(),
            status=TaskStatus.PENDING,
            priority=priority,
            planned_date=planned_date,
            due_at=ensure_utc(due_at) if due_at else None,
            created_at=utc_now,
            updated_at=utc_now,
            completed_at=None,
            cancelled_at=None,
        )


@dataclass(frozen=True)
class ReminderRule:
    id: str
    task_id: str | None
    message: str
    kind: ReminderKind
    offset_seconds: int | None
    weekdays_mask: int | None
    time_of_day: str | None
    timezone: str
    grace_seconds: int | None
    desktop_enabled: bool
    email_enabled: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReminderKind):
            raise TypeError("提醒规则类型必须是 ReminderKind。")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))


@dataclass(frozen=True)
class ReminderOccurrence:
    id: str
    rule_id: str
    task_id: str | None
    scheduled_at: datetime
    expires_at: datetime | None
    status: ReminderOccurrenceStatus
    skip_reason: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReminderOccurrenceStatus):
            raise TypeError("提醒实例状态必须是 ReminderOccurrenceStatus。")
        for field_name in ("scheduled_at", "expires_at", "created_at", "updated_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value))


@dataclass(frozen=True)
class NotificationDelivery:
    id: str
    occurrence_id: str
    channel: DeliveryChannel
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime | None
    claimed_at: datetime | None
    claim_token: str | None
    sent_at: datetime | None
    last_error_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.channel, DeliveryChannel):
            raise TypeError("通知渠道必须是 DeliveryChannel。")
        if not isinstance(self.status, DeliveryStatus):
            raise TypeError("通知投递状态必须是 DeliveryStatus。")
        for field_name in ("next_attempt_at", "claimed_at", "sent_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value))


@dataclass(frozen=True)
class WeeklySummaryRun:
    id: str
    iso_year: int
    iso_week: int
    week_start: date
    week_end: date
    status: WeeklySummaryStatus
    provider: str | None
    snapshot_sha256: str | None
    draft_path: str | None
    git_commit_sha: str | None
    last_error_code: str | None
    created_at: datetime
    generated_at: datetime | None
    approved_at: datetime | None
    published_at: datetime | None
    cleaned_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, WeeklySummaryStatus):
            raise TypeError("周总结状态必须是 WeeklySummaryStatus。")
        for field_name in (
            "created_at",
            "generated_at",
            "approved_at",
            "published_at",
            "cleaned_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value))
