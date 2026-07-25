from __future__ import annotations

import json
import os
import sqlite3
import threading
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.tasks.backup as backup_module
from app.tasks.backup import DatabaseBackupService
from app.tasks.errors import ConfirmationRequired


def _insert_marker(database, key: str, value: str) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO maintenance_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, "2026-07-25T00:00:00Z"),
        )


def test_backup_uses_sqlite_snapshot_including_committed_wal_data(
    task_database, tmp_path: Path, fixed_now: datetime
) -> None:
    _insert_marker(task_database, "wal-data", "included")
    service = DatabaseBackupService(task_database, tmp_path / "backups")

    backup = service.create_backup(reason="migration", now=fixed_now)

    assert isinstance(backup, Path)
    assert backup.exists()
    assert backup.name.endswith("-migration.db")
    connection = sqlite3.connect(backup)
    try:
        assert connection.execute(
            "SELECT value FROM maintenance_state WHERE key = 'wal-data'"
        ).fetchone()[0] == "included"
    finally:
        connection.close()
    assert service.valid_backups() == (backup,)


def test_backup_keeps_only_valid_newest_files_by_count_and_capacity(
    task_database, tmp_path: Path, fixed_now: datetime
) -> None:
    by_count = DatabaseBackupService(task_database, tmp_path / "count", max_backups=7)
    for offset in range(8):
        by_count.create_backup(
            reason="migration", now=fixed_now + timedelta(microseconds=offset)
        )
    assert len(by_count.valid_backups()) == 7

    by_size = DatabaseBackupService(
        task_database, tmp_path / "size", max_backups=10, max_total_bytes=1
    )
    by_size.create_backup(reason="migration", now=fixed_now)
    by_size.create_backup(reason="cleanup", now=fixed_now + timedelta(seconds=1))
    assert len(by_size.valid_backups()) == 1


def test_invalid_backup_is_not_listed_and_failed_new_backup_does_not_rotate_old(
    task_database, tmp_path: Path, fixed_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups", max_backups=1)
    old = service.create_backup(reason="migration", now=fixed_now)
    corrupt = service.backup_dir / "tasks-20260725T000000000000Z-corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    assert service.is_valid(corrupt) is False
    assert corrupt not in service.valid_backups()

    monkeypatch.setattr(task_database, "integrity_check", lambda path=None: False)
    with pytest.raises(RuntimeError, match="完整性"):
        service.create_backup(reason="cleanup", now=fixed_now + timedelta(seconds=1))
    assert old.exists()
    assert service.valid_backups() == ()
    assert not list(service.backup_dir.glob("*.tmp"))


def test_daily_backup_is_deduplicated_by_shanghai_calendar_day(
    task_database, tmp_path: Path
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups")
    first = service.create_backup(
        reason="daily", now=datetime(2026, 7, 25, 14, tzinfo=timezone.utc)
    )
    same_day = service.create_backup(
        reason="daily", now=datetime(2026, 7, 25, 15, tzinfo=timezone.utc)
    )
    next_day = service.create_backup(
        reason="daily", now=datetime(2026, 7, 25, 16, tzinfo=timezone.utc)
    )
    assert same_day == first
    assert next_day != first
    assert len(service.valid_backups()) == 2


def test_daily_backup_recovers_only_verified_stale_dead_process_lock(
    task_database, tmp_path: Path, fixed_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups")
    service.backup_dir.mkdir()
    lock_path = service.backup_dir / ".daily.lock"
    lock_path.write_text(
        json.dumps({"pid": 999_999_999, "created_at": "2000-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    ticks = iter((0.0, 6.0))
    monkeypatch.setattr(backup_module.time, "monotonic", lambda: next(ticks))

    backup = service.create_backup(reason="daily", now=fixed_now)

    assert backup.exists()
    assert not lock_path.exists()


def test_daily_backup_recovers_verified_stale_legacy_pid_lock(
    task_database, tmp_path: Path, fixed_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups")
    service.backup_dir.mkdir()
    lock_path = service.backup_dir / ".daily.lock"
    lock_path.write_text("999999999", encoding="utf-8")
    os.utime(lock_path, (946_684_800, 946_684_800))
    ticks = iter((0.0, 6.0))
    monkeypatch.setattr(backup_module.time, "monotonic", lambda: next(ticks))

    backup = service.create_backup(reason="daily", now=fixed_now)

    assert backup.exists()
    assert not lock_path.exists()


@pytest.mark.parametrize("payload", ["", "not valid lock metadata"])
def test_daily_backup_recovers_expired_invalid_lock_without_deleting_fresh_invalid_lock(
    task_database,
    tmp_path: Path,
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups")
    service.backup_dir.mkdir()
    lock_path = service.backup_dir / ".daily.lock"
    lock_path.write_text(payload, encoding="utf-8")
    os.utime(lock_path, (946_684_800, 946_684_800))
    ticks = iter((0.0, 6.0))
    monkeypatch.setattr(backup_module.time, "monotonic", lambda: next(ticks))

    backup = service.create_backup(reason="daily", now=fixed_now)

    assert backup.exists()
    assert not lock_path.exists()


def test_daily_backup_keeps_fresh_invalid_lock_and_times_out(
    task_database, tmp_path: Path, fixed_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups")
    service.backup_dir.mkdir()
    lock_path = service.backup_dir / ".daily.lock"
    lock_path.write_text("not valid lock metadata", encoding="utf-8")
    ticks = iter((0.0, 6.0))
    monkeypatch.setattr(backup_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(TimeoutError):
        service.create_backup(reason="daily", now=fixed_now)

    assert lock_path.exists()


def test_concurrent_same_timestamp_backups_reserve_distinct_target_paths(
    task_database, tmp_path: Path, fixed_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups")
    base_name = "tasks-20260725T040000000000Z-migration.db"
    original_exists = Path.exists
    barrier = threading.Barrier(2)

    def synchronized_exists(path: Path) -> bool:
        if path.name == base_name:
            barrier.wait(timeout=2)
            return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", synchronized_exists)
    paths: list[Path] = []
    errors: list[BaseException] = []

    def create() -> None:
        try:
            paths.append(service.create_backup(reason="migration", now=fixed_now))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=create)
    second = threading.Thread(target=create)
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)
    monkeypatch.setattr(Path, "exists", original_exists)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert all(path.exists() for path in paths)


@pytest.mark.parametrize("failure_stage", ["sidecar_move", "replace"])
def test_restore_failure_rolls_quarantined_database_files_back_to_original_names(
    task_database,
    tmp_path: Path,
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups")
    backup = service.create_backup(reason="migration", now=fixed_now)
    wal = task_database.path.with_name(f"{task_database.path.name}-wal")
    shm = task_database.path.with_name(f"{task_database.path.name}-shm")
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")

    if failure_stage == "sidecar_move":
        original_rename = backup_module.rename_with_retry

        def fail_wal_rename(source: Path, destination: Path) -> None:
            if source == wal:
                raise OSError("forced wal move failure")
            original_rename(source, destination)

        monkeypatch.setattr(backup_module, "rename_with_retry", fail_wal_rename)
    else:
        monkeypatch.setattr(
            backup_module,
            "replace_with_retry",
            lambda source, target: (_ for _ in ()).throw(OSError("forced replace failure")),
        )

    with pytest.raises(OSError, match="forced"):
        service.restore(backup, confirmed=True)

    assert task_database.path.exists()
    assert wal.exists()
    assert shm.exists()
    assert not list(task_database.path.parent.glob("tasks.db.quarantine-*"))


def test_restore_requires_confirmation_rejects_bad_input_and_quarantines_current_database(
    task_database, tmp_path: Path, fixed_now: datetime
) -> None:
    service = DatabaseBackupService(task_database, tmp_path / "backups")
    _insert_marker(task_database, "before", "snapshot")
    backup = service.create_backup(reason="migration", now=fixed_now)
    _insert_marker(task_database, "after", "current")
    bytes_before = task_database.path.read_bytes()

    with pytest.raises(ConfirmationRequired):
        service.restore(backup)
    assert task_database.path.read_bytes() == bytes_before

    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(b"bad")
    with pytest.raises(ValueError, match="有效"):
        service.restore(damaged, confirmed=True)
    assert task_database.path.read_bytes() == bytes_before

    # WAL/SHM 必须跟随隔离后的主库命名，才能组成一个可恢复的 SQLite 副本。
    task_database.path.with_name(f"{task_database.path.name}-wal").write_bytes(b"wal")
    task_database.path.with_name(f"{task_database.path.name}-shm").write_bytes(b"shm")
    assert service.restore(backup, confirmed=True) == task_database.path
    assert task_database.integrity_check()
    quarantine = next(task_database.path.parent.glob("tasks.db.quarantine-*"))
    assert quarantine.with_name(f"{quarantine.name}-wal").exists()
    assert quarantine.with_name(f"{quarantine.name}-shm").exists()
    with task_database.connect() as connection:
        assert connection.execute(
            "SELECT value FROM maintenance_state WHERE key = 'before'"
        ).fetchone()[0] == "snapshot"
        assert connection.execute(
            "SELECT 1 FROM maintenance_state WHERE key = 'after'"
        ).fetchone() is None


def test_shanghai_date_falls_back_to_fixed_utc_plus_eight_without_tzdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_timezone(_name: str):
        raise zoneinfo.ZoneInfoNotFoundError("no tzdata")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", missing_timezone)

    assert backup_module._shanghai_date(
        datetime(2026, 7, 25, 16, tzinfo=timezone.utc)
    ) == "2026-07-26"


def test_shanghai_date_does_not_hide_unexpected_zoneinfo_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_timezone(_name: str):
        raise RuntimeError("zoneinfo unexpectedly broken")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", broken_timezone)

    with pytest.raises(RuntimeError, match="unexpectedly broken"):
        backup_module._shanghai_date(datetime(2026, 7, 25, 16, tzinfo=timezone.utc))
