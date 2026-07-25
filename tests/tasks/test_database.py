import sqlite3
import threading
from pathlib import Path

import pytest

import app.tasks.database as database_module
from app.tasks.database import TaskDatabase
from app.tasks.errors import DatabaseCorruptError


def test_database_initializes_required_schema(tmp_path: Path) -> None:
    db = TaskDatabase(tmp_path / "tasks.db")
    db.initialize()
    with db.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "schema_migrations", "tasks", "task_events", "reminder_rules",
            "reminder_occurrences", "notification_deliveries",
            "weekly_summary_runs", "task_summary_archives",
            "maintenance_state",
        } <= tables
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            "idx_tasks_pending_due_at",
            "idx_tasks_pending_planned_date",
            "idx_reminder_occurrences_pending_scheduled_at",
            "idx_notification_deliveries_channel_status_next_attempt_at",
        } <= indexes


def test_initialize_is_idempotent_and_records_migration(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")

    database.initialize()
    database.initialize()

    with database.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in versions] == [1]


def test_initialize_supports_relative_database_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    database = TaskDatabase(Path("relative.db"))

    database.initialize()
    database.initialize()

    assert Path("relative.db").exists()
    with database.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in versions] == [1]


def test_failed_initializer_does_not_delete_other_initializer_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "tasks.db"
    initialization_lock_path = database_path.with_name(f"{database_path.name}.init.lock")
    first_database = TaskDatabase(database_path)
    second_database = TaskDatabase(database_path)
    original_quick_check = TaskDatabase._raise_if_quick_check_fails
    first_ready_to_fail = threading.Event()
    allow_first_failure = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()
    first_errors: list[BaseException] = []
    second_errors: list[BaseException] = []
    first_calls = 0
    original_connect = sqlite3.connect

    class CloseOnExitConnection(sqlite3.Connection):
        def __exit__(self, exc_type, exc_value, traceback):
            try:
                return super().__exit__(exc_type, exc_value, traceback)
            finally:
                self.close()

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = CloseOnExitConnection
        return original_connect(*args, **kwargs)

    def controlled_quick_check(connection: sqlite3.Connection) -> None:
        nonlocal first_calls
        if threading.current_thread().name == "initializer-a":
            first_calls += 1
            if first_calls == 2:
                first_ready_to_fail.set()
                assert allow_first_failure.wait(timeout=2)
                raise DatabaseCorruptError("force first initializer failure")
        original_quick_check(connection)

    monkeypatch.setattr(
        TaskDatabase,
        "_raise_if_quick_check_fails",
        staticmethod(controlled_quick_check),
    )
    monkeypatch.setattr(database_module.sqlite3, "connect", connect)

    def run_first() -> None:
        try:
            first_database.initialize()
        except BaseException as exc:
            first_errors.append(exc)

    def run_second() -> None:
        second_started.set()
        try:
            second_database.initialize()
        except BaseException as exc:
            second_errors.append(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=run_first, name="initializer-a")
    second_thread = threading.Thread(target=run_second, name="initializer-b")
    first_thread.start()
    assert first_ready_to_fail.wait(timeout=2)
    second_thread.start()
    assert second_started.wait(timeout=2)
    if not initialization_lock_path.exists():
        assert second_finished.wait(timeout=2)
    allow_first_failure.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert len(first_errors) == 1
    if second_errors:
        assert len(second_errors) == 1
        assert isinstance(second_errors[0], sqlite3.OperationalError)
        second_database.initialize()
    with second_database.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in versions] == [1]


def test_initialize_cleans_fresh_database_when_quick_check_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "interrupted.db"

    def interrupt_quick_check(connection: sqlite3.Connection) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        TaskDatabase,
        "_raise_if_quick_check_fails",
        staticmethod(interrupt_quick_check),
    )

    with pytest.raises(KeyboardInterrupt):
        TaskDatabase(database_path).initialize()

    assert not database_path.exists()
    assert not database_path.with_name(f"{database_path.name}-wal").exists()
    assert not database_path.with_name(f"{database_path.name}-shm").exists()


def test_initialize_keeps_locked_database_error_retryable(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    database.initialize()
    lock_connection = sqlite3.connect(database.path, timeout=0.1)
    try:
        lock_connection.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as error:
            database.initialize()
    finally:
        lock_connection.rollback()
        lock_connection.close()

    assert not isinstance(error.value, DatabaseCorruptError)
    assert database.integrity_check()


def test_transaction_commits_changes(task_database: TaskDatabase) -> None:
    with task_database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO maintenance_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("last_run", "completed", "2026-07-25T04:00:00Z"),
        )

    with task_database.connect() as connection:
        value = connection.execute(
            "SELECT value FROM maintenance_state WHERE key = ?", ("last_run",)
        ).fetchone()["value"]
    assert value == "completed"


def test_transaction_rolls_back_when_block_raises(task_database: TaskDatabase) -> None:
    with pytest.raises(RuntimeError, match="force rollback"):
        with task_database.transaction() as connection:
            connection.execute(
                "INSERT INTO maintenance_state (key, value, updated_at) VALUES (?, ?, ?)",
                ("last_run", "incomplete", "2026-07-25T04:00:00Z"),
            )
            raise RuntimeError("force rollback")

    with task_database.connect() as connection:
        row = connection.execute(
            "SELECT value FROM maintenance_state WHERE key = ?", ("last_run",)
        ).fetchone()
    assert row is None


def test_foreign_key_constraints_are_enforced(task_database: TaskDatabase) -> None:
    with task_database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events (id, task_id, event_type, occurred_at)
                VALUES (?, ?, ?, ?)
                """,
                ("event-1", "missing-task", "created", "2026-07-25T04:00:00Z"),
            )


def test_integrity_check_supports_default_and_override_paths(tmp_path: Path) -> None:
    database = TaskDatabase(tmp_path / "tasks.db")
    other_database = TaskDatabase(tmp_path / "other.db")
    database.initialize()
    other_database.initialize()

    assert database.integrity_check()
    assert database.integrity_check(other_database.path)


def test_initialize_raises_database_corrupt_error_when_quick_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_connect = sqlite3.connect
    fresh_path = tmp_path / "fresh.db"
    quick_check_results = iter(("ok", "not ok"))

    class QuickCheckResult:
        def __init__(self, value: str) -> None:
            self.value = value

        def fetchone(self) -> tuple[str]:
            return (self.value,)

    class QuickCheckConnection(sqlite3.Connection):
        def execute(self, statement: str, parameters: object = ()):
            if statement == "PRAGMA quick_check":
                return QuickCheckResult(next(quick_check_results))
            return super().execute(statement, parameters)

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = QuickCheckConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(database_module.sqlite3, "connect", connect)

    with pytest.raises(DatabaseCorruptError):
        TaskDatabase(fresh_path).initialize()

    assert not fresh_path.exists()
    assert not fresh_path.with_name(f"{fresh_path.name}-wal").exists()
    assert not fresh_path.with_name(f"{fresh_path.name}-shm").exists()


def test_initialize_preserves_existing_corrupt_database(tmp_path: Path) -> None:
    corrupt_path = tmp_path / "corrupt.db"
    original_bytes = b"not a sqlite database"
    corrupt_path.write_bytes(original_bytes)

    with pytest.raises(DatabaseCorruptError) as error:
        TaskDatabase(corrupt_path).initialize()

    assert isinstance(error.value.__cause__, sqlite3.DatabaseError)
    assert corrupt_path.read_bytes() == original_bytes


def test_initialize_closes_readonly_connection_for_corrupt_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupt_path = tmp_path / "corrupt.db"
    corrupt_path.write_bytes(b"not a sqlite database")
    original_connect = sqlite3.connect
    closed_connections: list[sqlite3.Connection] = []

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            closed_connections.append(self)
            super().close()

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = TrackingConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(database_module.sqlite3, "connect", connect)

    with pytest.raises(DatabaseCorruptError):
        TaskDatabase(corrupt_path).initialize()

    assert len(closed_connections) == 1
