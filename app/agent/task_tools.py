"""SQLite 任务与一次性提醒的 Agent 工具。

本模块没有 UI 或后台线程依赖。一次性提醒适配器只维护已经存在的
SQLite 规则、实例和投递记录；实际到期投递仍由后续调度器负责。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.agent.tools import Tool
from app.tasks.errors import ConfirmationRequired, TaskAssistantError
from app.tasks.models import DeliveryChannel, Task, to_utc_text
from app.tasks.repository import ReminderRepository
from app.tasks.service import (
    AmbiguousTaskReferenceError,
    InvalidTaskTransitionError,
    TaskNotFoundError,
    TaskService,
)


_PRIORITY_LABELS = {"high": "高", "normal": "普通", "low": "低"}
_TASK_TOOL_UNAVAILABLE = "任务服务未初始化，当前不能读写任务。"
_REMINDER_TOOL_UNAVAILABLE = "一次性提醒服务未初始化，当前不能读写提醒。"


class SQLiteOneTimeReminderAdapter:
    """Task7 的最小一次性提醒适配器，不负责调度或投递。"""

    def __init__(
        self,
        reminders: ReminderRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reminders = reminders
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        """返回当前带时区时间，供工具解析相对提醒时间。"""

        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("提醒服务时钟必须返回带时区时间。")
        return value.astimezone(timezone.utc)

    def add_reminder(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = _required_text(arguments, "text")
        repeat = arguments.get("repeat")
        if repeat is not None:
            raise ValueError("第一版提醒暂不支持 repeat，请传 null 或省略。")
        now = self.now()
        scheduled_at = _resolve_trigger_at(arguments, now)
        rule = self._reminders.create_one_time_rule(
            text,
            scheduled_at,
            channels=(DeliveryChannel.DESKTOP,),
            now=now,
        )
        pair = self._reminders.get_active_one_time_occurrence_for_rule(rule.id)
        if pair is None:
            raise RuntimeError("一次性提醒创建后未找到活动实例。")
        return {"reminder": _legacy_reminder_dict(*pair)}

    def list_reminders(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "reminders": [
                _legacy_reminder_dict(rule, occurrence)
                for rule, occurrence in self._reminders.list_active_one_time_occurrences()
            ]
        }

    def cancel_reminder(self, arguments: dict[str, Any]) -> dict[str, Any]:
        reminder_id = _required_text(arguments, "id")
        pair = self._reminders.cancel_one_time_occurrence(reminder_id, self.now())
        if pair is None:
            raise ValueError(f"未找到活动提醒：{reminder_id}")
        return {"reminder": _legacy_reminder_dict(*pair)}

    def task_reminder_status(self, task_id: str) -> str:
        count = self._reminders.count_active_occurrences_for_task(task_id)
        return f"待发送提醒 {count} 条" if count else "无待发送提醒"


def create_task_tools(
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None = None,
    summary_service: object | None = None,
) -> list[Tool]:
    """创建任务生命周期工具；未注入服务时返回明确失败的兼容工具。"""

    del summary_service
    return [
        Tool(
            name="task_create",
            description=(
                "创建任务。相对日期或相对截止时间必须先调用 get_current_time，"
                "再换算为带时区的 ISO 时间。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题。"},
                    "details": {"type": "string", "description": "任务详情。"},
                    "priority": {
                        "type": "string",
                        "enum": ["high", "normal", "low"],
                        "description": "优先级，默认 normal。",
                    },
                    "planned_date": {
                        "type": ["string", "null"],
                        "description": "计划日期 YYYY-MM-DD。相对日期须先查询当前时间。",
                    },
                    "due_at": {
                        "type": ["string", "null"],
                        "description": "带时区的 ISO 截止时间。相对时间须先查询当前时间。",
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            handler=lambda arguments: _create_task(task_service, reminder_scheduler, arguments),
            group="tasks",
        ),
        Tool(
            name="task_update",
            description=(
                "更新任务标题、详情、优先级、计划日期或截止时间。"
                "相对日期必须先调用 get_current_time 再换算。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_ref": {"type": "string", "description": "任务 ID 或唯一精确标题。"},
                    "title": {"type": "string", "description": "新标题。"},
                    "details": {"type": "string", "description": "新详情。"},
                    "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                    "planned_date": {"type": ["string", "null"], "description": "YYYY-MM-DD 或 null。"},
                    "due_at": {"type": ["string", "null"], "description": "带时区 ISO 时间或 null。"},
                },
                "required": ["task_ref"],
                "additionalProperties": False,
            },
            handler=lambda arguments: _update_task(task_service, reminder_scheduler, arguments),
            group="tasks",
        ),
        _task_transition_tool(
            "task_complete", "完成任务", task_service, reminder_scheduler, "complete_task"
        ),
        _task_transition_tool(
            "task_cancel", "取消任务", task_service, reminder_scheduler, "cancel_task"
        ),
        _task_transition_tool(
            "task_reopen", "重新打开任务", task_service, reminder_scheduler, "reopen_task"
        ),
        Tool(
            name="task_query",
            description="查询任务。scope 仅支持 today、pending 或 overdue。",
            parameters={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["today", "pending", "overdue"],
                        "description": "查询范围，默认 pending。",
                    },
                },
                "additionalProperties": False,
            },
            handler=lambda arguments: _query_tasks(task_service, reminder_scheduler, arguments),
            group="tasks",
        ),
    ]


def create_compatibility_task_tools(
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None = None,
) -> list[Tool]:
    """为旧 Agent 提供映射到 SQLite 服务的工具名。"""

    return [
        Tool(
            name="add_todo",
            description="兼容旧待办工具：创建一条 SQLite 任务。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "待办内容。"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=lambda arguments: _add_todo(task_service, reminder_scheduler, arguments),
            group="tasks",
        ),
        Tool(
            name="list_todos",
            description="兼容旧待办工具：列出 SQLite 中未完成任务。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda arguments: _list_todos(task_service, reminder_scheduler, arguments),
            group="tasks",
        ),
        Tool(
            name="complete_todo",
            description="兼容旧待办工具：完成一条 SQLite 任务。",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "任务 ID 或唯一标题。"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=lambda arguments: _complete_todo(task_service, reminder_scheduler, arguments),
            group="tasks",
        ),
        Tool(
            name="add_reminder",
            description=(
                "兼容旧提醒工具：创建一次性 SQLite 提醒。相对时间使用 delay_seconds "
                "或 delay_minutes；明确日期时间使用 trigger_at。repeat 仅支持 null。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "提醒内容。"},
                    "trigger_at": {"type": "string", "description": "带时区 ISO 提醒时间。"},
                    "delay_seconds": {"type": "number", "description": "延迟秒数。"},
                    "delay_minutes": {"type": "number", "description": "延迟分钟数。"},
                    "repeat": {"type": ["null"], "description": "第一版只支持 null。"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=lambda arguments: _reminder_action(reminder_scheduler, "add_reminder", arguments),
            group="tasks",
        ),
        Tool(
            name="list_reminders",
            description="兼容旧提醒工具：只列出活动的一次性 SQLite 提醒。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda arguments: _reminder_action(reminder_scheduler, "list_reminders", arguments),
            group="tasks",
        ),
        Tool(
            name="cancel_reminder",
            description="兼容旧提醒工具：取消该 SQLite 提醒实例及未完成投递。",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "提醒实例 ID。"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=lambda arguments: _reminder_action(reminder_scheduler, "cancel_reminder", arguments),
            group="tasks",
        ),
    ]


def _create_task(
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    service = _require_task_service(task_service)
    try:
        task = service.create_task(
            _required_text(arguments, "title"),
            **{key: arguments[key] for key in ("details", "priority", "planned_date", "due_at") if key in arguments},
        )
    except (TaskAssistantError, TypeError, ValueError) as exc:
        _raise_safe_task_error(exc)
    return _task_result("已创建", task, reminder_scheduler)


def _update_task(
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    service = _require_task_service(task_service)
    try:
        task = service.update_task(
            _required_text(arguments, "task_ref"),
            **{key: arguments[key] for key in ("title", "details", "priority", "planned_date", "due_at") if key in arguments},
        )
    except (TaskAssistantError, TypeError, ValueError) as exc:
        _raise_safe_task_error(exc)
    return _task_result("已更新", task, reminder_scheduler)


def _task_transition_tool(
    name: str,
    action: str,
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
    method_name: str,
) -> Tool:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        service = _require_task_service(task_service)
        try:
            task = getattr(service, method_name)(_required_text(arguments, "task_ref"))
        except (TaskAssistantError, TypeError, ValueError) as exc:
            _raise_safe_task_error(exc)
        return _task_result(f"已{action}", task, reminder_scheduler)

    return Tool(
        name=name,
        description=f"{action}一条任务。",
        parameters={
            "type": "object",
            "properties": {"task_ref": {"type": "string", "description": "任务 ID 或唯一精确标题。"}},
            "required": ["task_ref"],
            "additionalProperties": False,
        },
        handler=handler,
        group="tasks",
    )


def _query_tasks(
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    service = _require_task_service(task_service)
    scope = arguments.get("scope", "pending")
    if scope not in {"today", "pending", "overdue"}:
        raise ValueError("scope 只支持 today、pending 或 overdue。")
    now = service.now()
    if scope == "today":
        today = service.query_today(now)
        tasks = (*today.overdue, *today.due_today, *today.planned_today)
    else:
        pending = service.list_pending_tasks()
        tasks = (
            tuple(task for task in pending if task.due_at is not None and task.due_at < now)
            if scope == "overdue"
            else pending
        )
    payloads = [_task_payload(task, reminder_scheduler) for task in tasks]
    details = "\n".join(payload["display"] for payload in payloads)
    return {
        "scope": scope,
        "tasks": payloads,
        "message": "\n".join(
            part for part in (f"查询到 {len(payloads)} 条{_scope_label(scope)}任务。", details) if part
        ),
    }


def _add_todo(
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return _create_task(task_service, reminder_scheduler, {"title": arguments.get("text")})


def _list_todos(
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
    _arguments: dict[str, Any],
) -> dict[str, Any]:
    return _query_tasks(task_service, reminder_scheduler, {"scope": "pending"})


def _complete_todo(
    task_service: TaskService | None,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    service = _require_task_service(task_service)
    try:
        task = service.complete_task(_required_text(arguments, "id"))
    except (TaskAssistantError, TypeError, ValueError) as exc:
        _raise_safe_task_error(exc)
    return _task_result("已完成", task, reminder_scheduler)


def _reminder_action(
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
    method_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if reminder_scheduler is None:
        raise RuntimeError(_REMINDER_TOOL_UNAVAILABLE)
    try:
        return getattr(reminder_scheduler, method_name)(arguments)
    except (TaskAssistantError, TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def _task_result(
    verb: str,
    task: Task,
    reminder_scheduler: SQLiteOneTimeReminderAdapter | None,
) -> dict[str, Any]:
    payload = _task_payload(task, reminder_scheduler)
    return {
        "task": payload,
        "message": f"任务{verb}。{payload['display']}",
    }


def _task_payload(task: Task, reminder_scheduler: SQLiteOneTimeReminderAdapter | None) -> dict[str, Any]:
    reminder_status = (
        reminder_scheduler.task_reminder_status(task.id)
        if reminder_scheduler is not None
        else "提醒服务未接入"
    )
    payload = {
        "id": task.id,
        "title": task.title,
        "details": task.details,
        "status": task.status.value,
        "priority": task.priority.value,
        "planned_date": task.planned_date.isoformat() if task.planned_date else None,
        "due_at": to_utc_text(task.due_at) if task.due_at else None,
        "created_at": to_utc_text(task.created_at),
        "updated_at": to_utc_text(task.updated_at),
        "completed_at": to_utc_text(task.completed_at) if task.completed_at else None,
        "cancelled_at": to_utc_text(task.cancelled_at) if task.cancelled_at else None,
        "reminder_status": reminder_status,
    }
    payload["display"] = (
        f"标题：{task.title}；优先级：{_PRIORITY_LABELS[task.priority.value]}；"
        f"计划日期：{payload['planned_date'] or '未设置'}；"
        f"截止时间：{payload['due_at'] or '未设置'}；"
        f"提醒状态：{reminder_status}。"
    )
    return payload


def _legacy_reminder_dict(rule: object, occurrence: object) -> dict[str, Any]:
    return {
        "id": occurrence.id,
        "text": rule.message,
        "trigger_at": occurrence.scheduled_at.isoformat(timespec="seconds"),
        "repeat": None,
        "created_at": occurrence.created_at.isoformat(timespec="seconds"),
        "completed_at": (
            occurrence.updated_at.isoformat(timespec="seconds")
            if occurrence.status.value == "completed"
            else None
        ),
        "cancelled_at": (
            occurrence.updated_at.isoformat(timespec="seconds")
            if occurrence.status.value == "cancelled"
            else None
        ),
    }


def _resolve_trigger_at(arguments: dict[str, Any], now: datetime) -> datetime:
    delay_seconds = _optional_number(arguments, "delay_seconds")
    delay_minutes = _optional_number(arguments, "delay_minutes")
    trigger_at = arguments.get("trigger_at")
    if delay_seconds is not None or delay_minutes is not None:
        total_seconds = (delay_seconds or 0.0) + (delay_minutes or 0.0) * 60
        if total_seconds <= 0:
            raise ValueError("相对提醒时间必须大于 0 秒。")
        return now + timedelta(seconds=total_seconds)
    if isinstance(trigger_at, str) and trigger_at.strip():
        from app.tasks.models import parse_utc

        parsed = parse_utc(trigger_at)
        if parsed <= now:
            raise ValueError("提醒时间必须晚于当前时间。相对时间请使用 delay_seconds 或 delay_minutes。")
        return parsed
    raise ValueError("缺少提醒时间：请提供 trigger_at、delay_seconds 或 delay_minutes。")


def _optional_number(arguments: dict[str, Any], key: str) -> float | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 必须是数字。")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{key} 必须是有限数字。")
    return result


def _required_text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"缺少必填参数：{key}")
    return value.strip()


def _require_task_service(task_service: TaskService | None) -> TaskService:
    if task_service is None:
        raise RuntimeError(_TASK_TOOL_UNAVAILABLE)
    return task_service


def _raise_safe_task_error(exc: Exception) -> None:
    if isinstance(exc, AmbiguousTaskReferenceError):
        raise ValueError("任务标题不唯一，请提供任务 ID。") from exc
    if isinstance(exc, TaskNotFoundError):
        raise ValueError("未找到任务，请提供正确的任务 ID 或唯一标题。") from exc
    if isinstance(exc, InvalidTaskTransitionError):
        raise ValueError("任务当前状态不支持此操作。") from exc
    if isinstance(exc, ConfirmationRequired):
        raise ValueError("截止时间已过去，需要确认后再操作。") from exc
    raise ValueError(str(exc)) from exc


def _scope_label(scope: str) -> str:
    return {"today": "今日", "pending": "待办", "overdue": "逾期"}[scope]
