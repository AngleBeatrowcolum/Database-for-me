"""任务提醒 SQLite 数据库的建库与完整性基础设施。"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.tasks.errors import DatabaseCorruptError


_SQLITE_TIMEOUT_SECONDS = 5.0
_INITIALIZATION_LOCK_SUFFIX = ".init.lock"
_OPERATION_LOCK_SUFFIX = ".operation.lock"
_LOCK_POLL_INTERVAL_SECONDS = 0.05


_CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL CHECK(length(trim(title)) > 0),
                details TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('pending','completed','cancelled')),
                priority TEXT NOT NULL DEFAULT 'normal'
                    CHECK(priority IN ('high','normal','low')),
                planned_date TEXT,
                due_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                cancelled_at TEXT,
                CHECK (
                    (status='pending' AND completed_at IS NULL AND cancelled_at IS NULL)
                    OR (status='completed' AND completed_at IS NOT NULL AND cancelled_at IS NULL)
                    OR (status='cancelled' AND completed_at IS NULL AND cancelled_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE TABLE task_events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                occurred_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE reminder_rules (
                id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                kind TEXT NOT NULL
                    CHECK(kind IN ('deadline_offset','one_time','weekly')),
                offset_seconds INTEGER,
                weekdays_mask INTEGER,
                time_of_day TEXT,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                grace_seconds INTEGER,
                desktop_enabled INTEGER NOT NULL DEFAULT 1,
                email_enabled INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE reminder_occurrences (
                id TEXT PRIMARY KEY,
                rule_id TEXT NOT NULL REFERENCES reminder_rules(id) ON DELETE CASCADE,
                task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                scheduled_at TEXT NOT NULL,
                expires_at TEXT,
                status TEXT NOT NULL
                    CHECK(status IN ('pending','completed','skipped','cancelled')),
                skip_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(rule_id, scheduled_at)
            )
            """,
            """
            CREATE TABLE notification_deliveries (
                id TEXT PRIMARY KEY,
                occurrence_id TEXT NOT NULL
                    REFERENCES reminder_occurrences(id) ON DELETE CASCADE,
                channel TEXT NOT NULL CHECK(channel IN ('desktop','email')),
                status TEXT NOT NULL
                    CHECK(status IN ('pending','sending','sent','failed','skipped')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                claimed_at TEXT,
                claim_token TEXT,
                sent_at TEXT,
                last_error_code TEXT,
                UNIQUE(occurrence_id, channel)
            )
            """,
            """
            CREATE TABLE weekly_summary_runs (
                id TEXT PRIMARY KEY,
                iso_year INTEGER NOT NULL,
                iso_week INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                week_end TEXT NOT NULL,
                status TEXT NOT NULL,
                provider TEXT,
                snapshot_sha256 TEXT,
                draft_path TEXT,
                git_commit_sha TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL,
                generated_at TEXT,
                approved_at TEXT,
                published_at TEXT,
                cleaned_at TEXT,
                UNIQUE(iso_year, iso_week)
            )
            """,
            """
            CREATE TABLE task_summary_archives (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                summary_run_id TEXT NOT NULL
                    REFERENCES weekly_summary_runs(id) ON DELETE CASCADE,
                task_updated_at TEXT NOT NULL,
                archived_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE maintenance_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX idx_tasks_pending_due_at
                ON tasks(due_at) WHERE status = 'pending'
            """,
            """
            CREATE INDEX idx_tasks_pending_planned_date
                ON tasks(planned_date) WHERE status = 'pending'
            """,
            """
            CREATE INDEX idx_reminder_occurrences_pending_scheduled_at
                ON reminder_occurrences(scheduled_at) WHERE status = 'pending'
            """,
            """
            CREATE INDEX idx_notification_deliveries_channel_status_next_attempt_at
                ON notification_deliveries(channel, status, next_attempt_at)
            """,
        ),
    ),
)


class TaskDatabase:
    """管理任务提醒数据库的连接、schema 与事务。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._operation_state_lock = threading.RLock()
        self._operation_owner_thread: int | None = None
        self._operation_depth = 0

    def connect(self) -> sqlite3.Connection:
        """创建已启用外键检查的数据库连接。"""

        self._wait_for_operation_barrier()
        connection = sqlite3.connect(
            self._database_uri(self.path, mode="rw"),
            timeout=_SQLITE_TIMEOUT_SECONDS,
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        """创建并升级数据库 schema，拒绝继续使用损坏数据库。"""

        with self.operation_barrier():
            self._initialize()

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_descriptor: int | None = None
        connection: sqlite3.Connection | None = None
        created_by_this_call = False
        failed = False
        existing_files: set[Path] = set()
        try:
            lock_descriptor = self._acquire_initialization_lock()
            existing_files = {path for path in self._database_files() if path.exists()}
            try:
                database_descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR
                )
            except FileExistsError:
                self._check_existing_database()
            else:
                created_by_this_call = True
                os.close(database_descriptor)

            try:
                connection = self.connect()
                self._raise_if_quick_check_fails(connection)
                connection.execute("PRAGMA journal_mode=WAL").fetchone()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if not self._schema_migrations_exists(connection):
                        connection.execute(_CREATE_SCHEMA_MIGRATIONS)

                    applied_versions = {
                        row["version"]
                        for row in connection.execute("SELECT version FROM schema_migrations")
                    }
                    for version, statements in _MIGRATIONS:
                        if version in applied_versions:
                            continue
                        for statement in statements:
                            connection.execute(statement)
                        connection.execute(
                            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                            (version, _utc_timestamp()),
                        )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                self._raise_if_quick_check_fails(connection)
            except sqlite3.OperationalError:
                raise
            except sqlite3.DatabaseError as exc:
                if self._is_corruption_error(exc):
                    raise DatabaseCorruptError("任务数据库完整性检查失败。") from exc
                raise
        except BaseException:
            failed = True
            raise
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                try:
                    if failed and created_by_this_call:
                        self._remove_new_database_files(existing_files)
                finally:
                    if lock_descriptor is not None:
                        self._release_initialization_lock(lock_descriptor)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def operation_barrier(self) -> Iterator[None]:
        """串行化备份/恢复，并让并发 ``connect`` 等待其关键区结束。"""

        current_thread = threading.get_ident()
        with self._operation_state_lock:
            if self._operation_owner_thread == current_thread:
                self._operation_depth += 1
                nested = True
            else:
                nested = False
        if nested:
            try:
                yield
            finally:
                with self._operation_state_lock:
                    self._operation_depth -= 1
            return

        handle = self._acquire_operation_lock()
        with self._operation_state_lock:
            self._operation_owner_thread = current_thread
            self._operation_depth = 1
        try:
            yield
        finally:
            with self._operation_state_lock:
                self._operation_depth = 0
                self._operation_owner_thread = None
            try:
                self._unlock_operation_file(handle)
            finally:
                handle.close()

    def integrity_check(self, path: Path | None = None) -> bool:
        target = path or self.path
        connection = sqlite3.connect(self._database_uri(target, mode="ro"), uri=True)
        try:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()

    @staticmethod
    def _schema_migrations_exists(connection: sqlite3.Connection) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is not None
        )

    def _check_existing_database(self) -> None:
        if not self.path.exists():
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self._database_uri(self.path, mode="ro"),
                timeout=_SQLITE_TIMEOUT_SECONDS,
                uri=True,
            )
            self._raise_if_quick_check_fails(connection)
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError as exc:
            if self._is_corruption_error(exc):
                raise DatabaseCorruptError("任务数据库完整性检查失败。") from exc
            raise
        finally:
            if connection is not None:
                connection.close()

    def _acquire_initialization_lock(self) -> int:
        deadline = time.monotonic() + _SQLITE_TIMEOUT_SECONDS
        lock_path = self._initialization_lock_path()
        while True:
            try:
                return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise sqlite3.OperationalError("任务数据库初始化正在进行中。")
                time.sleep(_LOCK_POLL_INTERVAL_SECONDS)

    def _release_initialization_lock(self, descriptor: int) -> None:
        try:
            os.close(descriptor)
        finally:
            self._initialization_lock_path().unlink(missing_ok=True)

    def _initialization_lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}{_INITIALIZATION_LOCK_SUFFIX}")

    def _operation_lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}{_OPERATION_LOCK_SUFFIX}")

    def _wait_for_operation_barrier(self) -> None:
        with self._operation_state_lock:
            if self._operation_owner_thread == threading.get_ident():
                return
        deadline = time.monotonic() + _SQLITE_TIMEOUT_SECONDS
        while True:
            handle = self._open_operation_lock_file()
            try:
                if self._try_lock_operation_file(handle):
                    self._unlock_operation_file(handle)
                    return
            finally:
                handle.close()
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError("任务数据库备份或恢复正在进行中。")
            time.sleep(_LOCK_POLL_INTERVAL_SECONDS)

    def _acquire_operation_lock(self):
        deadline = time.monotonic() + _SQLITE_TIMEOUT_SECONDS
        while True:
            handle = self._open_operation_lock_file()
            if self._try_lock_operation_file(handle):
                return handle
            handle.close()
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError("任务数据库备份或恢复正在进行中。")
            time.sleep(_LOCK_POLL_INTERVAL_SECONDS)

    def _open_operation_lock_file(self):
        lock_path = self._operation_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        return handle

    @staticmethod
    def _try_lock_operation_file(handle) -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    @staticmethod
    def _unlock_operation_file(handle) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _database_uri(path: Path, *, mode: str) -> str:
        return f"{Path(path).resolve().as_uri()}?mode={mode}"

    def _database_files(self) -> tuple[Path, Path, Path]:
        return (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        )

    def _remove_new_database_files(self, existing_files: set[Path]) -> None:
        for path in self._database_files():
            if path not in existing_files:
                path.unlink(missing_ok=True)

    @staticmethod
    def _is_corruption_error(error: sqlite3.DatabaseError) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "file is not a database",
                "file is encrypted or is not a database",
                "database disk image is malformed",
                "malformed database schema",
            )
        )

    @staticmethod
    def _raise_if_quick_check_fails(connection: sqlite3.Connection) -> None:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise DatabaseCorruptError("任务数据库完整性检查失败。")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
