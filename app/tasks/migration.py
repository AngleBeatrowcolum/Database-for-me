"""一次性将首版 JSON 待办和提醒导入任务 SQLite 数据库。"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.tasks.database import TaskDatabase
from app.tasks.models import (
    DeliveryChannel,
    DeliveryStatus,
    Priority,
    ReminderKind,
    ReminderOccurrenceStatus,
    ReminderRule,
    Task,
    TaskStatus,
    parse_utc,
    to_utc_text,
)
from app.tasks.repository import (
    TaskRepository,
    _ensure_occurrence,
    _insert_rule,
)


_COMPLETION_KEY = "legacy_json_v1_completed"
_COMPLETED_AT_KEY = "legacy_json_v1_completed_at"


@dataclass(frozen=True)
class MigrationResult:
    """不可变的迁移执行摘要。"""

    tasks_imported: int
    reminders_imported: int
    backup_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _LegacyTask:
    source_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    status: TaskStatus
    completed_at: datetime | None
    cancelled_at: datetime | None


@dataclass(frozen=True)
class _LegacyReminder:
    source_id: str
    text: str
    trigger_at: datetime
    created_at: datetime
    updated_at: datetime
    status: ReminderOccurrenceStatus
    completed_at: datetime | None
    cancelled_at: datetime | None
    channels: tuple[DeliveryChannel, ...]


class LegacyJsonMigrator:
    """将旧 JSON 格式作为一个不可重复的原子迁移导入。"""

    def __init__(
        self,
        database: TaskDatabase,
        legacy_data_dir: Path,
        *,
        backup_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.legacy_data_dir = Path(legacy_data_dir)
        self.backup_root = (
            Path(backup_root)
            if backup_root is not None
            else self.database.path.parent / "backups" / "legacy"
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, *, now: datetime | None = None) -> MigrationResult:
        """备份、验证并在单个 SQLite 事务内导入旧记录。"""

        if self._is_completed():
            return MigrationResult(0, 0)

        migration_now = _utc_now((lambda: now) if now is not None else self._clock)

        source_paths = (
            self.legacy_data_dir / "tasks.json",
            self.legacy_data_dir / "reminders.json",
        )
        backup_paths = self._backup_sources(source_paths, migration_now)
        tasks = _load_tasks(source_paths[0])
        reminders = _load_reminders(source_paths[1])
        completed_at = migration_now

        with self.database.transaction(immediate=True) as connection:
            # 一个并行迁移者可能在源备份完成后已经提交；此时绝不重复导入。
            marker = connection.execute(
                "SELECT value FROM maintenance_state WHERE key = ?", (_COMPLETION_KEY,)
            ).fetchone()
            if marker is not None and marker["value"] == "true":
                return MigrationResult(0, 0)

            tasks_imported = self._import_tasks(connection, tasks)
            reminders_imported = self._import_reminders(connection, reminders)
            connection.execute(
                """
                INSERT INTO maintenance_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (_COMPLETION_KEY, "true", to_utc_text(completed_at)),
            )
            connection.execute(
                """
                INSERT INTO maintenance_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (_COMPLETED_AT_KEY, to_utc_text(completed_at), to_utc_text(completed_at)),
            )
        return MigrationResult(tasks_imported, reminders_imported, backup_paths)

    def _is_completed(self) -> bool:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT value FROM maintenance_state WHERE key = ?", (_COMPLETION_KEY,)
            ).fetchone()
            return row is not None and row["value"] == "true"
        finally:
            connection.close()

    def _backup_sources(
        self, source_paths: tuple[Path, Path], migration_now: datetime
    ) -> tuple[Path, ...]:
        existing = tuple(path for path in source_paths if path.is_file())
        if not existing:
            return ()

        stamp = _backup_timestamp(migration_now)
        destination = self._new_backup_directory(stamp)
        copied: list[Path] = []
        target: Path | None = None
        try:
            for source in existing:
                target = destination / source.name
                _copy_without_overwrite(source, target)
                copied.append(target)
        except BaseException:
            # 只清理本次尚未完成的副本；绝不触及旧源文件或已有备份目录。
            cleanup_paths = [*copied]
            if target is not None and target not in cleanup_paths:
                cleanup_paths.append(target)
            for copied_path in cleanup_paths:
                try:
                    copied_path.unlink()
                except OSError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
        return tuple(copied)

    def _new_backup_directory(self, stamp: str) -> Path:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        candidate = self.backup_root / stamp
        suffix = 1
        while True:
            try:
                candidate.mkdir()
                return candidate
            except FileExistsError:
                candidate = self.backup_root / f"{stamp}-{suffix}"
                suffix += 1

    def _import_tasks(self, connection: Any, tasks: tuple[_LegacyTask, ...]) -> int:
        repository = TaskRepository(self.database)
        imported = 0
        for legacy in tasks:
            task_id = _legacy_id("task", legacy.source_id)
            exists = connection.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if exists is not None:
                continue
            task = Task(
                id=task_id,
                title=legacy.title,
                details="",
                status=legacy.status,
                priority=Priority.NORMAL,
                planned_date=None,
                due_at=None,
                created_at=legacy.created_at,
                updated_at=legacy.updated_at,
                completed_at=legacy.completed_at,
                cancelled_at=legacy.cancelled_at,
            )
            repository.insert(task, event_type="legacy_imported", connection=connection)
            imported += 1
        return imported

    def _import_reminders(self, connection: Any, reminders: tuple[_LegacyReminder, ...]) -> int:
        imported = 0
        for legacy in reminders:
            rule_id = _legacy_id("reminder-rule", legacy.source_id)
            exists = connection.execute(
                "SELECT 1 FROM reminder_rules WHERE id = ?", (rule_id,)
            ).fetchone()
            if exists is not None:
                continue
            channels = set(legacy.channels)
            rule = ReminderRule(
                id=rule_id,
                task_id=None,
                message=legacy.text,
                kind=ReminderKind.ONE_TIME,
                offset_seconds=None,
                weekdays_mask=None,
                time_of_day=None,
                timezone="Asia/Shanghai",
                grace_seconds=None,
                desktop_enabled=DeliveryChannel.DESKTOP in channels,
                email_enabled=DeliveryChannel.EMAIL in channels,
                enabled=legacy.status is ReminderOccurrenceStatus.PENDING,
                created_at=legacy.created_at,
                updated_at=legacy.updated_at,
            )
            _insert_rule(connection, rule)
            occurrence = _ensure_occurrence(
                connection, rule, legacy.trigger_at, legacy.created_at
            )
            if legacy.status is not ReminderOccurrenceStatus.PENDING:
                terminal_at = legacy.completed_at or legacy.cancelled_at or legacy.updated_at
                occurrence_status = legacy.status.value
                connection.execute(
                    """
                    UPDATE reminder_occurrences
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (occurrence_status, to_utc_text(terminal_at), occurrence.id),
                )
                if legacy.status is ReminderOccurrenceStatus.COMPLETED:
                    connection.execute(
                        """
                        UPDATE notification_deliveries
                        SET status = ?, sent_at = ?, next_attempt_at = NULL,
                            claimed_at = NULL, claim_token = NULL
                        WHERE occurrence_id = ?
                        """,
                        (DeliveryStatus.SENT.value, to_utc_text(terminal_at), occurrence.id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE notification_deliveries
                        SET status = ?, next_attempt_at = NULL, claimed_at = NULL,
                            claim_token = NULL, last_error_code = ?
                        WHERE occurrence_id = ?
                        """,
                        (DeliveryStatus.SKIPPED.value, "legacy_cancelled", occurrence.id),
                    )
            imported += 1
        return imported


def _load_tasks(path: Path) -> tuple[_LegacyTask, ...]:
    raw = _load_collection(path, "tasks")
    seen: set[str] = set()
    parsed: list[_LegacyTask] = []
    for item in raw:
        source_id = _required_text(item, "id")
        _check_unique_id(seen, source_id, "待办")
        text_value = item.get("text", item.get("title"))
        title = _required_value_text(text_value, "text/title")
        created_at = _required_time(item, "created_at")
        updated_at = _optional_time(item, "updated_at") or created_at
        status, completed_at, cancelled_at = _task_state(item, updated_at)
        parsed.append(
            _LegacyTask(
                source_id=source_id,
                title=title,
                created_at=created_at,
                updated_at=updated_at,
                status=status,
                completed_at=completed_at,
                cancelled_at=cancelled_at,
            )
        )
    return tuple(parsed)


def _load_reminders(path: Path) -> tuple[_LegacyReminder, ...]:
    raw = _load_collection(path, "reminders")
    seen: set[str] = set()
    parsed: list[_LegacyReminder] = []
    for item in raw:
        source_id = _required_text(item, "id")
        _check_unique_id(seen, source_id, "提醒")
        if "repeat" not in item or item["repeat"] is not None:
            raise ValueError("旧提醒仅支持 repeat 为 null。")
        created_at = _required_time(item, "created_at")
        updated_at = _optional_time(item, "updated_at") or created_at
        status, completed_at, cancelled_at = _reminder_state(item, updated_at)
        parsed.append(
            _LegacyReminder(
                source_id=source_id,
                text=_required_text(item, "text"),
                trigger_at=_required_time(item, "trigger_at"),
                created_at=created_at,
                updated_at=updated_at,
                status=status,
                completed_at=completed_at,
                cancelled_at=cancelled_at,
                channels=_reminder_channels(item),
            )
        )
    return tuple(parsed)


def _load_collection(path: Path, key: str) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"旧 {key} 文件不是有效 JSON：{path}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get(key), list):
        raise ValueError(f"旧 {key} 文件顶层必须包含 {key} 列表。")
    if any(not isinstance(item, dict) for item in raw[key]):
        raise ValueError(f"旧 {key} 列表中的记录必须是对象。")
    return tuple(raw[key])


def _task_state(
    item: dict[str, Any], updated_at: datetime
) -> tuple[TaskStatus, datetime | None, datetime | None]:
    completed_at = _optional_time(item, "completed_at")
    cancelled_at = _optional_time(item, "cancelled_at")
    if completed_at is not None and cancelled_at is not None:
        raise ValueError("旧待办不能同时完成和取消。")
    derived = (
        TaskStatus.COMPLETED
        if completed_at is not None
        else TaskStatus.CANCELLED
        if cancelled_at is not None
        else TaskStatus.PENDING
    )
    status_value = item.get("status")
    if status_value is not None:
        if not isinstance(status_value, str):
            raise ValueError("旧待办 status 必须是字符串。")
        try:
            explicit = TaskStatus(status_value)
        except ValueError as exc:
            raise ValueError("旧待办 status 无效。") from exc
        if explicit is not derived:
            raise ValueError("旧待办 status 与终态时间不一致。")
    if derived is TaskStatus.COMPLETED:
        return derived, completed_at, None
    if derived is TaskStatus.CANCELLED:
        return derived, None, cancelled_at
    return derived, None, None


def _reminder_state(
    item: dict[str, Any], updated_at: datetime
) -> tuple[ReminderOccurrenceStatus, datetime | None, datetime | None]:
    completed_at = _optional_time(item, "completed_at")
    cancelled_at = _optional_time(item, "cancelled_at")
    if completed_at is not None and cancelled_at is not None:
        raise ValueError("旧提醒不能同时完成和取消。")
    derived = (
        ReminderOccurrenceStatus.COMPLETED
        if completed_at is not None
        else ReminderOccurrenceStatus.CANCELLED
        if cancelled_at is not None
        else ReminderOccurrenceStatus.PENDING
    )
    status_value = item.get("status")
    if status_value is not None:
        if not isinstance(status_value, str):
            raise ValueError("旧提醒 status 必须是字符串。")
        if status_value == "pending":
            explicit = ReminderOccurrenceStatus.PENDING
        elif status_value == "completed":
            explicit = ReminderOccurrenceStatus.COMPLETED
        elif status_value == "cancelled":
            explicit = ReminderOccurrenceStatus.CANCELLED
        else:
            raise ValueError("旧提醒 status 无效。")
        if explicit is not derived:
            raise ValueError("旧提醒 status 与终态时间不一致。")
    return derived, completed_at, cancelled_at


def _reminder_channels(item: dict[str, Any]) -> tuple[DeliveryChannel, ...]:
    if "channels" in item:
        raw_channels = item["channels"]
        if not isinstance(raw_channels, list):
            raise ValueError("旧提醒 channels 必须是列表。")
        channels: list[DeliveryChannel] = []
        for value in raw_channels:
            if not isinstance(value, str):
                raise ValueError("旧提醒 channels 必须包含字符串。")
            try:
                channel = DeliveryChannel(value)
            except ValueError as exc:
                raise ValueError("旧提醒 channels 包含未知渠道。") from exc
            if channel in channels:
                raise ValueError("旧提醒 channels 不能重复。")
            channels.append(channel)
        return tuple(channels)
    desktop = item.get("desktop_enabled", True)
    email = item.get("email_enabled", False)
    if not isinstance(desktop, bool) or not isinstance(email, bool):
        raise ValueError("旧提醒渠道开关必须是布尔值。")
    channels: list[DeliveryChannel] = []
    if desktop:
        channels.append(DeliveryChannel.DESKTOP)
    if email:
        channels.append(DeliveryChannel.EMAIL)
    return tuple(channels)


def _required_text(item: dict[str, Any], key: str) -> str:
    return _required_value_text(item.get(key), key)


def _required_value_text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"旧记录缺少有效字段：{key}")
    return value.strip()


def _required_time(item: dict[str, Any], key: str) -> datetime:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"旧记录缺少有效时间字段：{key}")
    try:
        return parse_utc(value)
    except ValueError as exc:
        raise ValueError(f"旧记录时间字段无效：{key}") from exc


def _optional_time(item: dict[str, Any], key: str) -> datetime | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"旧记录时间字段无效：{key}")
    try:
        return parse_utc(value)
    except ValueError as exc:
        raise ValueError(f"旧记录时间字段无效：{key}") from exc


def _check_unique_id(seen: set[str], source_id: str, label: str) -> None:
    if source_id in seen:
        raise ValueError(f"旧 {label} ID 重复：{source_id}")
    seen.add(source_id)


def _legacy_id(kind: str, source_id: str) -> str:
    # 命名空间隔离旧短 ID，避免碰撞到 SQLite 首版中新建的 UUID/调用方 ID。
    return f"legacy-json-v1:{kind}:{source_id}"


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("迁移时钟必须返回带时区的时间。")
    return value.astimezone(timezone.utc)


def _backup_timestamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S%fZ")


def _copy_without_overwrite(source: Path, target: Path) -> None:
    """以 O_EXCL 创建备份，避免意外覆盖已存在的源备份。"""

    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
