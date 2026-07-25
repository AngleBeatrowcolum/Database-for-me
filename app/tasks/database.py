"""任务提醒 SQLite 数据库的建库与完整性基础设施。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.tasks.errors import DatabaseCorruptError


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

    def connect(self) -> sqlite3.Connection:
        """创建已启用外键检查的数据库连接。"""

        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        """创建并升级数据库 schema，拒绝继续使用损坏数据库。"""

        existing_files = {path for path in self._database_files() if path.exists()}
        self._check_existing_database()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        remove_new_files = False
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
        except DatabaseCorruptError:
            remove_new_files = self.path not in existing_files
            raise
        except sqlite3.DatabaseError as exc:
            remove_new_files = self.path not in existing_files
            raise DatabaseCorruptError("任务数据库完整性检查失败。") from exc
        finally:
            if connection is not None:
                connection.close()
            if remove_new_files:
                self._remove_new_database_files(existing_files)

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

    def integrity_check(self, path: Path | None = None) -> bool:
        target = path or self.path
        with sqlite3.connect(target) as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

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
        try:
            with sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro", uri=True
            ) as connection:
                self._raise_if_quick_check_fails(connection)
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorruptError("任务数据库完整性检查失败。") from exc

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
    def _raise_if_quick_check_fails(connection: sqlite3.Connection) -> None:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise DatabaseCorruptError("任务数据库完整性检查失败。")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
