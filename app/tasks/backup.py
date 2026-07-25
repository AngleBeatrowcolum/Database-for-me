"""任务 SQLite 数据库的本地一致性备份与确认恢复。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.storage.atomic import rename_with_retry, replace_with_retry
from app.tasks.database import TaskDatabase
from app.tasks.errors import ConfirmationRequired
from app.tasks.models import ensure_utc, parse_utc, to_utc_text


_DAILY_KEY_PREFIX = "database_backup_daily:"
_SAFE_REASON = re.compile(r"[^a-z0-9]+")
_LOCK_STALE_AFTER = timedelta(minutes=10)
_LOCK_UNLINK_ATTEMPTS = 5
_LOCK_UNLINK_INITIAL_DELAY_SECONDS = 0.05
_RETRYABLE_WINERRORS = {5, 32}


@dataclass(frozen=True)
class BackupResult:
    path: Path | None
    created: bool
    reason: str


class DatabaseBackupService:
    """使用 SQLite 原生 backup API 创建、轮换和恢复本地快照。"""

    def __init__(
        self,
        database: TaskDatabase,
        backup_dir: Path,
        *,
        max_backups: int = 7,
        max_total_bytes: int = 200 * 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_backups < 1:
            raise ValueError("max_backups 必须至少为 1。")
        if max_total_bytes < 1:
            raise ValueError("max_total_bytes 必须至少为 1。")
        self.database = database
        self.backup_dir = Path(backup_dir)
        self.max_backups = max_backups
        self.max_total_bytes = max_total_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create_backup(
        self, *, reason: str = "manual", now: datetime | None = None
    ) -> Path:
        """创建已验证的在线快照；失败时不轮换现有有效备份。"""

        timestamp = ensure_utc(now or self._clock())
        normalized_reason = _normalise_reason(reason)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if normalized_reason == "daily":
            result = self._create_daily(timestamp)
        else:
            result = self._create_snapshot(timestamp, normalized_reason)
        if result.created:
            self._prune_valid_backups()
        if result.path is None:
            raise RuntimeError("备份创建未返回文件路径。")
        return result.path

    # 简短别名便于调用方按动词使用服务，不改变创建语义。
    def create(self, *, reason: str = "manual", now: datetime | None = None) -> Path:
        return self.create_backup(reason=reason, now=now)

    def valid_backups(self) -> tuple[Path, ...]:
        """按旧到新返回完整性检查通过的正式备份。"""

        if not self.backup_dir.is_dir():
            return ()
        candidates = sorted(
            (path for path in self.backup_dir.glob("tasks-*.db") if path.is_file()),
            key=lambda path: path.name,
        )
        return tuple(path for path in candidates if self.is_valid(path))

    def is_valid(self, path: Path) -> bool:
        """安全检查正式 SQLite 备份，不创建或修改任何文件。"""

        candidate = Path(path)
        if candidate.suffix.lower() != ".db" or not candidate.is_file():
            return False
        return self._integrity_ok(candidate)

    def restore(self, path: Path, *, confirmed: bool = False) -> Path:
        """确认后将经过验证的备份原子恢复为当前任务数据库。"""

        if not confirmed:
            raise ConfirmationRequired("恢复数据库需要显式确认。")
        source = Path(path)
        if not self.is_valid(source):
            raise ValueError("所选备份不是有效的 SQLite 数据库。")

        target = self.database.path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._snapshot_file_to_temp(source, target.parent, target.name)
        moved: tuple[tuple[Path, Path], ...] = ()
        try:
            if not self._integrity_ok(temporary):
                raise RuntimeError("恢复临时数据库完整性检查失败。")
            moved = self._quarantine_current_database(target)
            replace_with_retry(temporary, target)
            temporary = None
        except BaseException:
            if moved:
                self._restore_quarantined_database(moved)
            raise
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass
        return target

    def _create_daily(self, timestamp: datetime) -> BackupResult:
        day = _shanghai_date(timestamp)
        key = f"{_DAILY_KEY_PREFIX}{day}"
        # SQLite 的 backup 在一个已开启的写事务上会等待自身的 WAL 锁；用同目录
        # 排他文件锁串行化 daily，同时 maintenance_state 负责跨重启的去重记录。
        with self._daily_lock():
            connection = self.database.connect()
            try:
                existing = connection.execute(
                    "SELECT value FROM maintenance_state WHERE key = ?", (key,)
                ).fetchone()
            finally:
                connection.close()
            if existing is not None:
                existing_path = Path(existing["value"])
                if self.is_valid(existing_path):
                    return BackupResult(existing_path, False, "daily")
            result = self._create_snapshot(timestamp, "daily")
            with self.database.transaction(immediate=True) as connection:
                # 锁应已排除竞争者；保留检查以防手工状态更新后误覆盖记录。
                existing = connection.execute(
                    "SELECT value FROM maintenance_state WHERE key = ?", (key,)
                ).fetchone()
                if existing is not None and self.is_valid(Path(existing["value"])):
                    if result.path is not None:
                        result.path.unlink()
                    return BackupResult(Path(existing["value"]), False, "daily")
                connection.execute(
                    """
                    INSERT INTO maintenance_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, str(result.path), to_utc_text(timestamp)),
                )
            return result

    @contextmanager
    def _daily_lock(self):
        lock_path = self.backup_dir / ".daily.lock"
        descriptor: int | None = None
        deadline = time.monotonic() + 5.0
        while descriptor is None:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                is_reclaimable = self._is_verified_stale_lock(
                    lock_path
                ) or self._is_expired_invalid_lock(lock_path)
                if is_reclaimable and self._unlink_with_retry(lock_path):
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待每日备份锁超时。")
                time.sleep(0.05)
        try:
            metadata = json.dumps(
                {
                    "pid": os.getpid(),
                    "created_at": to_utc_text(datetime.now(timezone.utc)),
                },
                sort_keys=True,
            ).encode("utf-8")
            os.write(descriptor, metadata)
            os.fsync(descriptor)
            yield
        finally:
            os.close(descriptor)
            self._unlink_with_retry(lock_path)

    def _create_snapshot(self, timestamp: datetime, reason: str) -> BackupResult:
        source = self.database.connect()
        try:
            return self._create_snapshot_from_connection(source, timestamp, reason)
        finally:
            source.close()

    def _create_snapshot_from_connection(
        self, source: sqlite3.Connection, timestamp: datetime, reason: str
    ) -> BackupResult:
        target = self._reserve_backup_path(timestamp, reason)
        reserved = True
        temporary = self._new_temp(self.backup_dir, f".{target.name}.")
        try:
            destination = sqlite3.connect(temporary)
            try:
                source.backup(destination)
            finally:
                destination.close()
            if not self._integrity_ok(temporary):
                raise RuntimeError("新备份完整性检查失败。")
            replace_with_retry(temporary, target)
            temporary = None
            reserved = False
            return BackupResult(target, True, reason)
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass
            if reserved:
                self._unlink_with_retry(target)

    def _reserve_backup_path(self, timestamp: datetime, reason: str) -> Path:
        stamp = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
        suffix = 0
        while True:
            suffix_text = "" if suffix == 0 else f"_{suffix}"
            candidate = self.backup_dir / f"tasks-{stamp}-{reason}{suffix_text}.db"
            try:
                descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                suffix += 1
                continue
            else:
                os.close(descriptor)
                return candidate

    def _prune_valid_backups(self) -> None:
        backups = list(self.valid_backups())
        total_bytes = sum(path.stat().st_size for path in backups)
        while len(backups) > 1 and (
            len(backups) > self.max_backups or total_bytes > self.max_total_bytes
        ):
            oldest = backups.pop(0)
            size = oldest.stat().st_size
            oldest.unlink()
            total_bytes -= size

    def _snapshot_file_to_temp(self, source: Path, directory: Path, target_name: str) -> Path:
        temporary = self._new_temp(directory, f".{target_name}.restore-")
        source_connection: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source_connection = sqlite3.connect(
                f"{source.resolve().as_uri()}?mode=ro", uri=True
            )
            destination = sqlite3.connect(temporary)
            source_connection.backup(destination)
            destination.close()
            destination = None
            source_connection.close()
            source_connection = None
            return temporary
        except BaseException:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
            raise
        finally:
            if destination is not None:
                destination.close()
            if source_connection is not None:
                source_connection.close()

    def _quarantine_current_database(self, target: Path) -> tuple[tuple[Path, Path], ...]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        while True:
            quarantined_database = target.with_name(
                f"{target.name}.quarantine-{stamp}"
            )
            names = (
                quarantined_database,
                quarantined_database.with_name(f"{quarantined_database.name}-wal"),
                quarantined_database.with_name(f"{quarantined_database.name}-shm"),
            )
            if not any(path.exists() for path in names):
                break
            stamp = f"{stamp}-1"
        sources = (
            target,
            target.with_name(f"{target.name}-wal"),
            target.with_name(f"{target.name}-shm"),
        )
        moved: list[tuple[Path, Path]] = []
        try:
            for source, destination in zip(sources, names, strict=True):
                if not source.exists():
                    continue
                rename_with_retry(source, destination)
                moved.append((source, destination))
        except BaseException:
            self._restore_quarantined_database(tuple(moved))
            raise
        return tuple(moved)

    @staticmethod
    def _restore_quarantined_database(moved: tuple[tuple[Path, Path], ...]) -> None:
        for source, destination in reversed(moved):
            if source.exists() or not destination.exists():
                continue
            try:
                rename_with_retry(destination, source)
            except OSError:
                # 如果 Windows 句柄仍阻止回滚，原文件仍安全保留在隔离路径。
                continue

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _is_verified_stale_lock(self, lock_path: Path) -> bool:
        metadata = self._lock_metadata(lock_path)
        if metadata is None:
            return False
        pid, created_at = metadata
        if datetime.now(timezone.utc) - created_at < _LOCK_STALE_AFTER:
            return False
        return not self._is_process_alive(pid)

    @staticmethod
    def _lock_metadata(lock_path: Path) -> tuple[int, datetime] | None:
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                pid = data["pid"]
                created_at = parse_utc(data["created_at"])
            elif isinstance(data, int) and not isinstance(data, bool):
                # 首版锁文件只写 PID；仅同时满足 mtime 超期和 PID 已死才回收。
                pid = data
                created_at = datetime.fromtimestamp(
                    lock_path.stat().st_mtime, timezone.utc
                )
            else:
                return None
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        return pid, created_at

    def _is_expired_invalid_lock(self, lock_path: Path) -> bool:
        if self._lock_metadata(lock_path) is not None:
            return False
        try:
            modified_at = datetime.fromtimestamp(lock_path.stat().st_mtime, timezone.utc)
        except OSError:
            return False
        return datetime.now(timezone.utc) - modified_at >= _LOCK_STALE_AFTER

    @staticmethod
    def _unlink_with_retry(path: Path) -> bool:
        for attempt in range(_LOCK_UNLINK_ATTEMPTS):
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return True
            except OSError as exc:
                if (
                    getattr(exc, "winerror", None) not in _RETRYABLE_WINERRORS
                    or attempt == _LOCK_UNLINK_ATTEMPTS - 1
                ):
                    return False
                time.sleep(_LOCK_UNLINK_INITIAL_DELAY_SECONDS * (2**attempt))
        return False

    def _integrity_ok(self, path: Path) -> bool:
        try:
            return self.database.integrity_check(Path(path))
        except (OSError, sqlite3.DatabaseError):
            return False

    @staticmethod
    def _new_temp(directory: Path, prefix: str) -> Path:
        descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
        os.close(descriptor)
        return Path(name)


def _normalise_reason(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("备份原因必须是非空字符串。")
    normalized = _SAFE_REASON.sub("-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("备份原因不包含安全文件名字符。")
    return normalized[:48]


def _shanghai_date(value: datetime) -> str:
    utc_value = ensure_utc(value)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        return utc_value.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    except ZoneInfoNotFoundError:
        # Windows 精简 Python 可能没有 tzdata；上海在本产品规则中固定 UTC+8。
        return (utc_value + timedelta(hours=8)).date().isoformat()
