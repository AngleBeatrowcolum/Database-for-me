"""旧提醒存储 API 的 SQLite 兼容门面。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
import warnings


@dataclass(frozen=True)
class ScheduledReminder:
    """保留旧公开数据类型，供现有插件导入。"""

    id: str
    text: str
    trigger_at: str
    repeat: None
    created_at: str
    completed_at: str | None = None
    cancelled_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "trigger_at": self.trigger_at,
            "repeat": self.repeat,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "cancelled_at": self.cancelled_at,
        }


class _OneTimeReminderFacade(Protocol):
    def add_reminder(self, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def list_reminders(self, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def cancel_reminder(self, arguments: dict[str, Any]) -> dict[str, Any]: ...


class ReminderStore:
    """已废弃：将旧提醒调用转发给注入的 SQLite 一次性提醒适配器。

    该类不再读取或写入 ``reminders.json``。它仅保留 add/list/cancel 的旧
    Agent API；到期检查与标记投递属于后续调度器，不能在此门面中模拟。
    """

    def __init__(self, scheduler: _OneTimeReminderFacade) -> None:
        warnings.warn(
            "ReminderStore 已废弃；请改用 SQLiteOneTimeReminderAdapter。",
            DeprecationWarning,
            stacklevel=2,
        )
        self._set_scheduler(scheduler)

    @classmethod
    def from_sqlite_adapter(cls, scheduler: _OneTimeReminderFacade) -> "ReminderStore":
        """供启动组装兼容属性时使用，不在正常启动中发出废弃告警。"""

        instance = cls.__new__(cls)
        instance._set_scheduler(scheduler)
        return instance

    def _set_scheduler(self, scheduler: _OneTimeReminderFacade) -> None:
        if not all(
            callable(getattr(scheduler, method, None))
            for method in ("add_reminder", "list_reminders", "cancel_reminder")
        ):
            raise TypeError(
                "ReminderStore 已废弃，必须注入 SQLite 一次性提醒适配器；"
                "不再支持 JSON 文件路径。"
            )
        self._scheduler = scheduler

    def add_reminder(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._scheduler.add_reminder(arguments)

    def list_reminders(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._scheduler.list_reminders(arguments)

    def cancel_reminder(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._scheduler.cancel_reminder(arguments)

    def due_reminders(self, *_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        """兼容旧 UI 轮询：Task7 尚未提供到期调度，因此不触发任何提醒。"""

        return []

    def mark_completed(self, reminder_id: str) -> dict[str, Any]:
        """兼容旧 UI 回调；绝不改写 SQLite 实例或任务状态。"""

        return {"id": str(reminder_id), "status": "not_scheduled"}
