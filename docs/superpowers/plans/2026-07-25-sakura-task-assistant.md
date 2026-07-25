# Sakura Task Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-first Sakura task assistant that records and queries work, delivers desktop and QQ email reminders, produces confirmed weekly summaries, and safely archives and cleans old data.

**Architecture:** A focused `app/tasks` domain owns SQLite and task state, while `app/notifications` and `app/summaries` consume repository interfaces. Sakura injects these services at bootstrap and keeps `pet_window.py` limited to polling prepared desktop events. Independent Windows workers reuse the same services without importing PySide6.

**Tech Stack:** Python 3.12+, stdlib `sqlite3`, `zoneinfo`, `smtplib`, `subprocess`, PySide6, `keyring`, pytest, DeepSeek OpenAI-compatible HTTP API, Git, Windows Task Scheduler.

---

## Scope and delivery order

This plan implements the approved design in three working checkpoints:

1. Local task database, work query, migration, and backup.
2. Desktop/QQ reminder delivery and Windows scheduling.
3. Weekly summary, confirmed private GitHub publication, and safe cleanup.

Each checkpoint must leave the application usable and all tests green. Personal profile storage, web-form filling, blog deployment, server reminder mode, and trading-day calendars remain outside this plan.

## File map

### New production files

```text
app/tasks/__init__.py                 Domain exports
app/tasks/errors.py                   Stable domain and confirmation exceptions
app/tasks/models.py                   Enums, dataclasses, UTC/Shanghai helpers
app/tasks/settings.py                 Non-secret task assistant configuration
app/tasks/database.py                 SQLite connections, schema, transactions
app/tasks/repository.py               Task/reminder/summary persistence
app/tasks/service.py                  Task state machine and reminder consistency
app/tasks/today_query.py              Deterministic today query and presentation
app/tasks/scheduler.py                Occurrence generation and delivery claiming
app/tasks/migration.py                Legacy JSON import
app/tasks/backup.py                   SQLite online backup, rotation, restore
app/notifications/__init__.py         Notification exports
app/notifications/credentials.py      Windows Credential Manager adapter
app/notifications/qq_mail.py          QQ SMTP transport and templates
app/notifications/dispatcher.py       Channel-specific delivery state machine
app/summaries/__init__.py             Summary exports
app/summaries/models.py               Snapshot and structured summary models
app/summaries/snapshot.py             Deterministic weekly fact extraction
app/summaries/renderer.py             Markdown renderer and archive manifest
app/summaries/service.py              Generate, approve, publish, archive, clean
app/summaries/providers/__init__.py   Provider exports
app/summaries/providers/base.py       SummaryProvider protocol
app/summaries/providers/deepseek.py   DeepSeek V4 Pro JSON provider
app/summaries/providers/local_fallback.py Deterministic offline provider
app/integrations/__init__.py          Integration exports
app/integrations/git_publisher.py     Private-repo validation and exact-file push
app/workers/__init__.py               Worker package
app/workers/reminder_worker.py        Short-lived email/reminder command
app/workers/weekly_summary_worker.py  Short-lived weekly summary command
app/platforms/reminder_task.py        Windows Task Scheduler XML and registration
app/platforms/weekly_summary_task.py  Weekly Task Scheduler XML and registration
app/agent/task_tools.py               Sakura task and summary tool definitions
app/ui/task_reminder_bridge.py        Qt-free prepared-reminder presentation bridge
tools/configure_task_assistant.py     Secret-safe local setup command
setup-task-assistant.bat              Windows setup entry point
```

### Existing files to modify

```text
.gitignore
requirements.txt
app/storage/paths.py
app/agent/__init__.py
app/agent/builtin_tools.py
app/agent/reminders.py
app/core/app_context.py
app/core/bootstrap.py
app/ui/pet_window.py
README.md
```

### New test files

```text
tests/conftest.py
tests/fakes.py
tests/tasks/test_models.py
tests/tasks/test_database.py
tests/tasks/test_repository.py
tests/tasks/test_service.py
tests/tasks/test_today_query.py
tests/tasks/test_migration.py
tests/tasks/test_backup.py
tests/tasks/test_scheduler.py
tests/agent/test_task_tools.py
tests/notifications/test_credentials.py
tests/notifications/test_qq_mail.py
tests/notifications/test_dispatcher.py
tests/platforms/test_reminder_task.py
tests/summaries/test_snapshot.py
tests/summaries/test_renderer.py
tests/summaries/test_deepseek.py
tests/summaries/test_service.py
tests/integrations/test_git_publisher.py
tests/ui/test_task_reminder_bridge.py
tests/workers/test_workers.py
tests/test_task_assistant_e2e.py
```

## Task 1: Import the Sakura source baseline

**Files:**
- Add: `.gitignore`
- Add: `LICENSE`
- Add: `VERSION`
- Add: `app/`
- Add: `plugins/`
- Add: `third_party/`
- Add: `tools/`
- Add: `main.py`
- Add: `requirements.txt`
- Add: Windows batch files and update metadata

- [ ] **Step 1: Verify ignored runtime and personal data**

Run:

```bash
git check-ignore runtime data
git ls-files --others --exclude-standard | rg '(^|/)(data|runtime|target|__pycache__)/' && exit 1 || true
```

Expected: `runtime` and `data` are reported as ignored; the second command prints nothing.

- [ ] **Step 2: Compile the unmodified Python source**

Run:

```bash
python3 -m compileall -q app main.py plugins tools/studio
```

Expected: exit code 0.

- [ ] **Step 3: Stage only the source allowlist**

Run:

```bash
git add .gitignore LICENSE VERSION app install.bat main.py plugins requirements.txt \
  start.bat start_studio.bat third_party tools update-delete.json update.bat
git diff --cached --check
git diff --cached --name-only | rg '(^|/)(data|runtime|target)/' && exit 1 || true
```

Expected: whitespace check passes and no ignored runtime/data/build output is staged.

- [ ] **Step 4: Commit the clean baseline**

```bash
git commit -m "chore: import Sakura 0.9.9 source"
```

## Task 2: Add task models, settings, and storage paths

**Files:**
- Create: `app/tasks/__init__.py`
- Create: `app/tasks/errors.py`
- Create: `app/tasks/models.py`
- Create: `app/tasks/settings.py`
- Modify: `app/storage/paths.py`
- Create: `tests/conftest.py`
- Create: `tests/tasks/test_models.py`

- [ ] **Step 1: Write failing model and path tests**

```python
# tests/tasks/test_models.py
from datetime import datetime, timezone
from pathlib import Path

from app.storage.paths import StoragePaths
from app.tasks.models import Priority, Task, TaskStatus, parse_utc, to_utc_text
from app.tasks.settings import TaskAssistantSettings


def test_task_defaults_and_utc_round_trip(tmp_path: Path) -> None:
    now = datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)
    task = Task.new("完成实验报告", now=now)
    assert task.status is TaskStatus.PENDING
    assert task.priority is Priority.NORMAL
    assert parse_utc(to_utc_text(now)) == now

    paths = StoragePaths(tmp_path)
    assert paths.tasks_database() == tmp_path / "data" / "tasks.db"
    assert paths.task_assistant_config() == tmp_path / "data" / "config" / "task_assistant.json"
    assert paths.task_database_backup_dir == tmp_path / "data" / "backups" / "sqlite"


def test_task_assistant_settings_are_non_secret() -> None:
    settings = TaskAssistantSettings()
    data = settings.to_dict()
    assert data["timezone"] == "Asia/Shanghai"
    assert data["smtp_host"] == "smtp.qq.com"
    assert "password" not in data
    assert "api_key" not in data
```

Create the fixed clock fixture at the same time:

```python
# tests/conftest.py
from datetime import datetime, timezone
import pytest


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run the test and verify import failure**

Run:

```bash
python3 -m pytest tests/tasks/test_models.py -q
```

Expected: collection fails because `app.tasks` does not exist.

- [ ] **Step 3: Implement enums, immutable task models, and time helpers**

`app/tasks/models.py` must define these exact public names:

```python
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

    @classmethod
    def new(cls, title: str, *, now: datetime, details: str = "",
            priority: Priority = Priority.NORMAL,
            planned_date: date | None = None,
            due_at: datetime | None = None) -> "Task":
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
```

Also define `ReminderRule`, `ReminderOccurrence`, `NotificationDelivery`,
`WeeklySummaryRun`, `ensure_utc`, `to_utc_text`, `parse_utc`, and
`local_date_for_utc`. All aware datetimes are normalized to UTC; naive
datetimes raise `ValueError`.

`app/tasks/errors.py` defines:

```python
class TaskAssistantError(RuntimeError):
    pass


class ConfirmationRequired(TaskAssistantError):
    pass


class DatabaseCorruptError(TaskAssistantError):
    pass
```

- [ ] **Step 4: Implement non-secret JSON settings and paths**

`TaskAssistantSettings` must contain:

```python
@dataclass(frozen=True)
class TaskAssistantSettings:
    timezone: str = "Asia/Shanghai"
    email_enabled: bool = False
    qq_email: str = ""
    recipient_email: str = ""
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    summary_enabled: bool = True
    summary_repo_path: str = ""
    summary_repo_slug: str = "AngleBeatrowcolum/personal-weekly-summaries"
    deepseek_enabled: bool = False
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
```

Use `atomic_write_text` for saves. Add `tasks_database`,
`task_assistant_config`, `task_database_backup_dir`,
`weekly_summary_drafts_dir`, and `weekly_summary_snapshots_dir` to
`StoragePaths`, and create those directories from `ensure_dirs`.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/tasks/test_models.py -q
git add app/tasks app/storage/paths.py tests/conftest.py tests/tasks/test_models.py
git commit -m "feat: add task domain models and settings"
```

Expected: tests pass.

## Task 3: Create the SQLite database and schema

**Files:**
- Create: `app/tasks/database.py`
- Create: `tests/tasks/test_database.py`

- [ ] **Step 1: Write the failing database test**

```python
from pathlib import Path

from app.tasks.database import TaskDatabase


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
```

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest tests/tasks/test_database.py -q
```

Expected: import failure for `app.tasks.database`.

- [ ] **Step 3: Implement `TaskDatabase`**

Use stdlib `sqlite3`, `row_factory = sqlite3.Row`, `timeout=5.0`,
`PRAGMA foreign_keys=ON`, and `PRAGMA busy_timeout=5000`. The first migration
must create the exact tables and checks below:

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

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
);

CREATE TABLE task_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    occurred_at TEXT NOT NULL
);

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
);

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
);

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
);

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
);

CREATE TABLE task_summary_archives (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    summary_run_id TEXT NOT NULL
        REFERENCES weekly_summary_runs(id) ON DELETE CASCADE,
    task_updated_at TEXT NOT NULL,
    archived_at TEXT NOT NULL
);

CREATE TABLE maintenance_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Add indexes for pending tasks by `due_at` and `planned_date`, pending
occurrences by `scheduled_at`, and deliveries by
`(channel, status, next_attempt_at)`.

Extend `tests/conftest.py` with:

```python
from app.tasks.database import TaskDatabase


@pytest.fixture
def task_database(tmp_path):
    database = TaskDatabase(tmp_path / "tasks.db")
    database.initialize()
    return database
```

- [ ] **Step 4: Add transaction and integrity APIs**

Implement:

```python
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
```

`initialize()` runs `PRAGMA quick_check` after migrations and raises
`DatabaseCorruptError` instead of creating or overwriting data when the result
is not `ok`.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/tasks/test_database.py -q
git add app/tasks/database.py tests/conftest.py tests/tasks/test_database.py
git commit -m "feat: add task sqlite schema"
```

## Task 4: Implement repositories and atomic delivery claiming

**Files:**
- Create: `app/tasks/repository.py`
- Create: `tests/tasks/test_repository.py`

- [ ] **Step 1: Write failing repository tests**

```python
def test_task_round_trip_and_unique_delivery_claim(task_database, fixed_now) -> None:
    tasks = TaskRepository(task_database)
    reminders = ReminderRepository(task_database)
    task = Task.new("完成实验报告", now=fixed_now, due_at=fixed_now + timedelta(days=1))
    tasks.insert(task, event_type="created")
    assert tasks.get(task.id) == task

    rule = reminders.create_deadline_rule(task, offset_seconds=-7200, now=fixed_now)
    occurrence = reminders.ensure_occurrence(
        rule, scheduled_at=task.due_at - timedelta(hours=2), now=fixed_now
    )
    first = reminders.claim_due_deliveries(
        DeliveryChannel.EMAIL, now=task.due_at, limit=10
    )
    second = reminders.claim_due_deliveries(
        DeliveryChannel.EMAIL, now=task.due_at, limit=10
    )
    assert [item.occurrence_id for item in first] == [occurrence.id]
    assert second == []
```

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest tests/tasks/test_repository.py -q
```

Expected: import failure for repository classes.

- [ ] **Step 3: Implement `TaskRepository`**

Implement exact operations:

```text
insert(task, event_type)
get(task_id)
find_pending_by_exact_title(title)
list_pending()
update(task, event_type, before)
delete(task_id)
clear_archive_marker(task_id)
list_events_between(start, end)
```

Every mutation writes `task_events` in the same transaction. Convert rows only
through functions in `models.py`; JSON fields use sorted UTF-8 JSON. Every
mutating repository method accepts an optional active SQLite connection so a
service can coordinate task and reminder writes in one transaction.

- [ ] **Step 4: Implement `ReminderRepository` and lease tokens**

Implement:

```text
create_deadline_rule(task, offset_seconds, now)
create_one_time_rule(message, scheduled_at, channels, now)
create_weekly_rule(message, weekdays_mask, time_of_day, grace_seconds, now)
ensure_occurrence(rule, scheduled_at, now)
cancel_pending_for_task(task_id, now)
claim_due_deliveries(channel, now, limit, lease_seconds=300)
mark_delivery_sent(delivery_id, claim_token, sent_at)
mark_delivery_failed(delivery_id, claim_token, error_code, next_attempt_at)
mark_delivery_skipped(delivery_id, claim_token, reason)
```

Claiming uses one `BEGIN IMMEDIATE` transaction. It first recovers `sending`
rows whose `claimed_at` is older than the lease, then assigns one UUID
`claim_token` per selected row. Write-back SQL must include both `id` and
`claim_token`.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/tasks/test_repository.py -q
git add app/tasks/repository.py tests/tasks/test_repository.py
git commit -m "feat: add task and reminder repositories"
```

## Task 5: Implement task state transitions and today queries

**Files:**
- Create: `app/tasks/service.py`
- Create: `app/tasks/today_query.py`
- Create: `tests/tasks/test_service.py`
- Create: `tests/tasks/test_today_query.py`

- [ ] **Step 1: Write failing state-machine tests**

```python
def test_create_update_complete_and_reopen(task_service, fixed_now) -> None:
    created = task_service.create_task(
        title="实验报告",
        priority="high",
        planned_date="2026-07-26",
        due_at="2026-07-26T07:00:00Z",
        now=fixed_now,
    )
    assert created.priority.value == "high"
    assert len(task_service.reminders.rules_for_task(created.id)) == 2

    completed = task_service.complete_task(created.id, now=fixed_now + timedelta(hours=1))
    assert completed.status.value == "completed"
    assert task_service.reminders.pending_for_task(created.id) == []

    reopened = task_service.reopen_task(created.id, now=fixed_now + timedelta(hours=2))
    assert reopened.status.value == "pending"
```

- [ ] **Step 2: Write failing deterministic query test**

```python
def test_today_query_deduplicates_and_orders(task_service, fixed_now) -> None:
    overdue = task_service.create_task(
        title="逾期高优先级", priority="high",
        planned_date="2026-07-25", due_at="2026-07-25T03:00:00Z",
        now=fixed_now - timedelta(days=1), allow_past_due=True,
    )
    due = task_service.create_task(
        title="今天截止", priority="normal",
        due_at="2026-07-25T09:00:00Z", now=fixed_now,
    )
    planned = task_service.create_task(
        title="今天计划", priority="low",
        planned_date="2026-07-25", now=fixed_now,
    )
    result = task_service.query_today(now=fixed_now)
    assert [item.id for item in result.overdue] == [overdue.id]
    assert [item.id for item in result.due_today] == [due.id]
    assert [item.id for item in result.planned_today] == [planned.id]
    assert result.summary.total == 3
```

- [ ] **Step 3: Verify failures**

```bash
python3 -m pytest tests/tasks/test_service.py tests/tasks/test_today_query.py -q
```

- [ ] **Step 4: Implement service and query**

`TaskService` accepts `TaskDatabase`, `TaskRepository`, `ReminderRepository`,
and a clock. It wraps each public mutation in
`database.transaction(immediate=True)` and passes that connection to both
repositories. On task creation, create two deadline rules (`-86400`, `-7200`)
in the same transaction. On due date changes, cancel unsent occurrences before
creating replacements. On every mutation, remove the old
`task_summary_archives` row.

`TodayQueryService.query(now)` must implement:

```python
overdue = due_at is not None and due_at < now
due_today = not overdue and local_date_for_utc(due_at, SHANGHAI) == today
planned_today = not overdue and not due_today and planned_date == today
priority_rank = {"high": 0, "normal": 1, "low": 2}
```

Return immutable `TodayQueryResult` with `display_text()` and `speech_text()`.
The full display contains every task once; speech contains counts and the
nearest deadline.

Extend `tests/conftest.py` with real service fixtures:

```python
from app.tasks.repository import ReminderRepository, TaskRepository
from app.tasks.service import TaskService


@pytest.fixture
def task_service(task_database):
    task_repository = TaskRepository(task_database)
    reminder_repository = ReminderRepository(task_database)
    return TaskService(task_database, task_repository, reminder_repository)
```

When `due_at <= now` and `allow_past_due=True`, save the task as overdue but
do not create default deadline occurrences. When `allow_past_due=False`,
raise `ConfirmationRequired`.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/tasks/test_service.py tests/tasks/test_today_query.py -q
git add app/tasks/service.py app/tasks/today_query.py tests/tasks/test_service.py \
  tests/tasks/test_today_query.py tests/conftest.py
git commit -m "feat: add task service and today query"
```

## Task 6: Migrate JSON and add rolling SQLite backups

**Files:**
- Create: `app/tasks/migration.py`
- Create: `app/tasks/backup.py`
- Create: `tests/tasks/test_migration.py`
- Create: `tests/tasks/test_backup.py`

- [ ] **Step 1: Write failing migration tests**

```python
def test_legacy_json_migration_is_idempotent(tmp_path, task_database, fixed_now) -> None:
    (tmp_path / "tasks.json").write_text(
        '{"tasks":[{"id":"abc12345","text":"旧任务","created_at":"2026-07-01T08:00:00+08:00","completed_at":null}]}',
        encoding="utf-8",
    )
    (tmp_path / "reminders.json").write_text(
        '{"reminders":[{"id":"rem12345","text":"旧提醒","trigger_at":"2026-07-26T14:00:00+08:00","repeat":null,"created_at":"2026-07-01T08:00:00+08:00","completed_at":null,"cancelled_at":null}]}',
        encoding="utf-8",
    )
    migrator = LegacyJsonMigrator(task_database, legacy_data_dir=tmp_path)
    assert migrator.run(now=fixed_now).tasks_imported == 1
    assert migrator.run(now=fixed_now).tasks_imported == 0
```

- [ ] **Step 2: Write failing backup rotation test**

```python
def test_backup_keeps_seven_files_under_200_mb(task_database, tmp_path, fixed_now) -> None:
    service = DatabaseBackupService(
        task_database,
        backup_dir=tmp_path / "backups",
        max_backups=7,
        max_total_bytes=200 * 1024 * 1024,
    )
    paths = [
        service.create_backup(now=fixed_now + timedelta(days=day), reason="daily")
        for day in range(8)
    ]
    assert not paths[0].exists()
    assert len(service.valid_backups()) == 7
    assert all(service.is_valid(path) for path in service.valid_backups())
```

- [ ] **Step 3: Verify failures**

```bash
python3 -m pytest tests/tasks/test_migration.py tests/tasks/test_backup.py -q
```

- [ ] **Step 4: Implement migration and backup**

`LegacyJsonMigrator` copies source JSON into
`data/backups/legacy/<timestamp>/` before one transactional import. Store
`legacy_json_v1_completed=true` in `maintenance_state`. Legacy reminders become
`one_time` rules and occurrences.

`DatabaseBackupService` uses `sqlite3.Connection.backup`, writes
`.tmp`, validates with `PRAGMA integrity_check`, then calls
`replace_with_retry`. The configured total limit is 200 MB. Rotation runs only
after the new backup is valid:

```python
while len(backups) > self.max_backups or total_size(backups) > self.max_total_bytes:
    if len(backups) == 1:
        break
    backups[0].unlink()
    backups = backups[1:]
```

`restore(path, confirmed=False)` raises `ConfirmationRequired` unless
`confirmed` is true. It quarantines the current DB, validates a temporary
restored DB, then atomically replaces `tasks.db`.

- [ ] **Step 5: Run checkpoint-one tests and commit**

```bash
python3 -m pytest tests/tasks -q
git add app/tasks/migration.py app/tasks/backup.py tests/tasks/test_migration.py \
  tests/tasks/test_backup.py
git commit -m "feat: migrate legacy tasks and back up sqlite"
```

Expected: all task-domain tests pass.

## Task 7: Register task tools and inject services

**Files:**
- Create: `app/agent/task_tools.py`
- Modify: `app/agent/builtin_tools.py`
- Modify: `app/agent/reminders.py`
- Modify: `app/agent/__init__.py`
- Modify: `app/core/app_context.py`
- Modify: `app/core/bootstrap.py`
- Create: `tests/agent/test_task_tools.py`

- [ ] **Step 1: Write failing tool registration test**

```python
def test_task_tools_are_registered(task_service, tmp_path) -> None:
    scheduler = ReminderScheduler(task_service.reminders)
    registry = create_builtin_tool_registry(
        tmp_path,
        task_service=task_service,
        reminder_scheduler=scheduler,
    )
    assert {
        "task_create", "task_update", "task_complete", "task_cancel",
        "task_reopen", "task_query", "add_todo", "list_todos",
        "complete_todo", "add_reminder", "list_reminders", "cancel_reminder",
    } <= {tool.name for tool in registry.all()}
```

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest tests/agent/test_task_tools.py -q
```

- [ ] **Step 3: Implement task tool definitions**

`create_task_tools(task_service, reminder_scheduler, summary_service=None)`
returns `Tool` instances. `task_create` accepts `title`, `details`, `priority`,
`planned_date`, `due_at`, and `allow_past_due`. `task_query.scope` is one of
`today`, `pending`, or `overdue`. Tool descriptions require calling
`get_current_time` before translating relative dates.

Compatibility tools map as follows:

```text
add_todo(text)        -> task_create(title=text)
list_todos()          -> task_query(scope=pending)
complete_todo(id)     -> task_complete(task_ref=id)
add_reminder(text, trigger_at, delay_seconds, delay_minutes, repeat)
                       -> create one_time rule and occurrence
list_reminders()      -> list active one_time occurrences
cancel_reminder(id)   -> cancel occurrence and its deliveries
```

- [ ] **Step 4: Replace JSON construction in bootstrap**

Create `TaskDatabase`, initialize it, run legacy migration, then construct
repositories, `TaskService`, and `ReminderScheduler`. Add them to
`StorageServices` and expose `AppContext.task_service` and
`AppContext.reminder_scheduler`. Keep `ReminderStore` as a thin deprecated
facade so imports from plugins do not break.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/agent/test_task_tools.py tests/tasks -q
git add app/agent app/core tests/agent/test_task_tools.py
git commit -m "feat: connect task services to Sakura tools"
```

## Task 8: Generate and claim reminder occurrences

**Files:**
- Create: `app/tasks/scheduler.py`
- Create: `tests/tasks/test_scheduler.py`

- [ ] **Step 1: Write deadline and weekly-boundary tests**

```python
def test_scheduler_coalesces_missed_deadline_offsets(scheduler, task_service, fixed_now) -> None:
    task = task_service.create_task(
        title="报告",
        due_at="2026-07-26T04:00:00Z",
        now=fixed_now - timedelta(days=2),
    )
    claimed = scheduler.claim_due(
        "email", now=fixed_now + timedelta(days=2), limit=10
    )
    assert len(claimed) == 1
    assert claimed[0].coalesced_count == 2


def test_weekly_occurrence_expires_after_thirty_minutes(scheduler) -> None:
    scheduler.ensure_stock_rule()
    before = scheduler.claim_due("email", now=shanghai("2026-07-27 14:29"))
    after = scheduler.claim_due("email", now=shanghai("2026-07-28 14:31"))
    assert len(before) == 1
    assert after == []
    assert scheduler.skipped_reason_for("2026-07-28") == "expired"
```

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest tests/tasks/test_scheduler.py -q
```

- [ ] **Step 3: Implement occurrence generation**

For each enabled rule:

```text
deadline_offset: scheduled_at = task.due_at + offset_seconds
one_time: occurrence already exists; never generate a second
weekly: generate one occurrence for matching Shanghai local date
```

Use `UNIQUE(rule_id, scheduled_at)` for idempotency. A weekly occurrence gets
`expires_at = scheduled_at + grace_seconds`.

- [ ] **Step 4: Implement deadline coalescing**

Before claiming, group overdue unsent deadline deliveries by `(task_id,
channel)`. Keep the newest occurrence as the delivery and mark older pending
ones `skipped/coalesced`. Return `PreparedDelivery` with deterministic
`display_text`, `speech_text`, original scheduled time, and
`coalesced_count`.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/tasks/test_scheduler.py tests/tasks/test_repository.py -q
git add app/tasks/scheduler.py tests/tasks/test_scheduler.py
git commit -m "feat: schedule and coalesce task reminders"
```

## Task 9: Bridge prepared desktop reminders into Sakura

**Files:**
- Create: `app/ui/task_reminder_bridge.py`
- Modify: `app/ui/pet_window.py`
- Create: `tests/ui/test_task_reminder_bridge.py`

- [ ] **Step 1: Write a focused bridge test**

```python
from types import SimpleNamespace

from app.ui.task_reminder_bridge import TaskReminderBridge


class FakeScheduler:
    def __init__(self, delivery):
        self.delivery = delivery
        self.sent = []

    def claim_due(self, channel, *, limit):
        return [self.delivery]

    def mark_delivery_sent(self, delivery_id, claim_token):
        self.sent.append((delivery_id, claim_token))


def test_bridge_uses_prepared_text_and_marks_only_delivery_sent() -> None:
    delivery = SimpleNamespace(
        delivery_id="delivery-1", claim_token="claim-1",
        display_text="高优先级：实验报告将在 15:00 截止。",
        speech_text="实验报告将在下午三点截止。",
    )
    scheduler = FakeScheduler(delivery)
    bridge = TaskReminderBridge(scheduler)
    prepared = bridge.poll()
    assert prepared.reply.translation == delivery.display_text
    assert prepared.reply.text == delivery.speech_text
    bridge.mark_shown(prepared)
    assert scheduler.sent == [("delivery-1", "claim-1")]
```

- [ ] **Step 2: Verify failure**

```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ui/test_task_reminder_bridge.py -q
```

- [ ] **Step 3: Replace the JSON reminder poll**

Keep `REMINDER_CHECK_INTERVAL_MS = 30_000`, but call
`TaskReminderBridge.poll()`. The bridge claims
`DeliveryChannel.DESKTOP`, builds an `AgentEvent(type="reminder_due")`, and
returns a deterministic `ChatReply` without calling the LLM. The existing
subtitle/TTS path remains responsible for showing and speaking.

The bridge returns:

```python
@dataclass(frozen=True)
class PreparedDesktopReminder:
    delivery_id: str
    claim_token: str
    event: AgentEvent
    reply: ChatReply
```

`reply` contains one `ChatSegment` whose `text` is the prepared speech and
whose `translation` is the authoritative full display text.

- [ ] **Step 4: Mark channel delivery, not task completion**

After the bubble is accepted by `_consume_agent_result`, call
`mark_delivery_sent(delivery_id, claim_token)`. If deterministic rendering
fails, mark only that delivery failed; do not complete the task. Remove the
old `ReminderStore.mark_completed` call from this path.

- [ ] **Step 5: Run test and commit**

```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ui/test_task_reminder_bridge.py -q
git add app/ui/task_reminder_bridge.py app/ui/pet_window.py \
  tests/ui/test_task_reminder_bridge.py
git commit -m "feat: show deterministic desktop task reminders"
```

## Task 10: Add credentials and QQ SMTP delivery

**Files:**
- Create: `app/notifications/__init__.py`
- Create: `app/notifications/credentials.py`
- Create: `app/notifications/qq_mail.py`
- Create: `app/notifications/dispatcher.py`
- Modify: `requirements.txt`
- Create: `tests/notifications/test_credentials.py`
- Create: `tests/notifications/test_qq_mail.py`
- Create: `tests/notifications/test_dispatcher.py`
- Create: `tests/fakes.py`

- [ ] **Step 1: Write credential and SMTP tests**

```python
def test_credentials_never_appear_in_repr() -> None:
    store = FakeCredentialStore({"qq-smtp": "secret-auth-code"})
    credential = store.get("qq-smtp")
    assert credential == "secret-auth-code"
    assert "secret-auth-code" not in repr(store)


def test_qq_mailer_uses_ssl_and_no_secret_in_error(fake_smtp) -> None:
    mailer = QQMailer(
        host="smtp.qq.com", port=465, sender="123456@qq.com",
        recipient="123456@qq.com", credential_store=FakeCredentialStore(
            {"qq-smtp": "secret-auth-code"}
        ), smtp_factory=fake_smtp,
    )
    mailer.send(subject="任务提醒", body="实验报告将在两小时后截止。")
    assert fake_smtp.login_calls == [("123456@qq.com", "secret-auth-code")]
    assert fake_smtp.sent_messages[0]["Subject"] == "任务提醒"
```

`tests/fakes.py` defines a mapping-backed `FakeCredentialStore` with `get` and
`set`, plus `FakeSMTP` with `login`, `send_message`, context-manager methods,
and recorded call lists. Its `__repr__` returns
`"FakeCredentialStore(<redacted>)"`.

- [ ] **Step 2: Verify failures**

```bash
python3 -m pytest tests/notifications -q
```

- [ ] **Step 3: Implement credential storage**

Add `keyring>=25.0` to `requirements.txt`. `KeyringCredentialStore` uses
service name `sakura-task-assistant` and usernames `qq-smtp` and
`deepseek-api`. On Windows, reject a keyring backend whose priority is not
positive. Never include returned values in `repr`, logs, or exceptions.

- [ ] **Step 4: Implement mail and dispatcher**

`QQMailer` uses `smtplib.SMTP_SSL` and `EmailMessage`. Classify
authentication errors as `auth`, temporary SMTP/network errors as
`temporary`, and invalid recipient errors as `permanent`.

`NotificationDispatcher.dispatch_email(limit=20)`:

```text
claim due email deliveries
send each message
sent -> mark sent
temporary and attempts < 3 -> failed with next attempt
auth/permanent or attempts >= 3 -> failed without automatic retry
```

Use 10, 30, and 60 minute retry delays for attempts one through three.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/notifications tests/tasks/test_scheduler.py -q
git add app/notifications requirements.txt tests/notifications tests/fakes.py
git commit -m "feat: deliver task reminders through QQ mail"
```

## Task 11: Add short-lived workers and Windows scheduled tasks

**Files:**
- Create: `app/workers/__init__.py`
- Create: `app/workers/reminder_worker.py`
- Create: `app/platforms/reminder_task.py`
- Create: `tools/configure_task_assistant.py`
- Create: `setup-task-assistant.bat`
- Create: `tests/workers/test_workers.py`
- Create: `tests/platforms/test_reminder_task.py`

- [ ] **Step 1: Write worker import and scheduler XML tests**

```python
def test_reminder_worker_does_not_import_qt() -> None:
    source = Path("app/workers/reminder_worker.py").read_text(encoding="utf-8")
    assert "PySide6" not in source
    assert "app.ui" not in source


def test_windows_task_xml_is_battery_safe(tmp_path: Path) -> None:
    xml = build_reminder_task_xml(
        python_exe=tmp_path / "runtime" / "python.exe",
        worker_path=tmp_path / "app" / "workers" / "reminder_worker.py",
        base_dir=tmp_path,
    )
    assert "<Interval>PT10M</Interval>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<WakeToRun>false</WakeToRun>" in xml
```

- [ ] **Step 2: Verify failures**

```bash
python3 -m pytest tests/workers/test_workers.py tests/platforms/test_reminder_task.py -q
```

- [ ] **Step 3: Implement the worker**

`python -m app.workers.reminder_worker --base-dir <path>` must:

```text
load non-secret settings
initialize database
run legacy migration if needed
create daily backup if due
generate occurrences
dispatch email deliveries
write a sanitized result to runtime log
exit 0 for an empty queue
exit nonzero for database/configuration failure
```

- [ ] **Step 4: Implement Task Scheduler registration and setup**

Generate UTF-16 Windows Task Scheduler XML with a ten-minute repetition,
logon trigger, `StartWhenAvailable=true`, battery execution enabled, and
`WakeToRun=false`. Register with:

```python
subprocess.run(
    ["schtasks.exe", "/Create", "/TN", r"Sakura\TaskReminderWorker",
     "/XML", str(xml_path), "/F"],
    check=True, capture_output=True, text=True,
)
```

The setup script collects non-secret email/repository settings, reads secrets
with `getpass.getpass`, saves them via `KeyringCredentialStore`, initializes
the DB, creates an initial backup, and registers scheduled tasks. It never
echoes secrets. It exposes explicit subcommands:

```text
configure
register-tasks
backup
list-backups
restore --backup <path> --confirm
```

Without the literal `--confirm`, `restore` prints the candidate backup and
exits without changing the database. With confirmation, it first runs
`schtasks.exe /End` for both Sakura worker task names and refuses to continue
while `data/sakura.lock` indicates the desktop application is running. It
restores atomically, then leaves both scheduled tasks registered for their
next normal trigger.

- [ ] **Step 5: Run tests and commit checkpoint two**

```bash
python3 -m pytest tests/tasks tests/notifications tests/workers tests/platforms \
  tests/agent tests/ui/test_task_reminder_bridge.py -q
git add app/workers app/platforms/reminder_task.py tools/configure_task_assistant.py \
  setup-task-assistant.bat tests/workers tests/platforms
git commit -m "feat: run reminders with Windows Task Scheduler"
```

## Task 12: Build deterministic weekly snapshots and local summaries

**Files:**
- Create: `app/summaries/__init__.py`
- Create: `app/summaries/models.py`
- Create: `app/summaries/snapshot.py`
- Create: `app/summaries/providers/__init__.py`
- Create: `app/summaries/providers/base.py`
- Create: `app/summaries/providers/local_fallback.py`
- Create: `tests/summaries/test_snapshot.py`

- [ ] **Step 1: Write snapshot tests**

```python
def test_snapshot_contains_facts_and_archive_items(summary_snapshot_service, fixed_now) -> None:
    snapshot = summary_snapshot_service.build(now=fixed_now)
    assert snapshot.iso_year == 2026
    assert snapshot.iso_week == 30
    assert snapshot.stats.created_count >= 0
    assert all(item.task_id and item.updated_at for item in snapshot.archive_items)
    assert snapshot.sha256 == snapshot.recalculate_sha256()
```

- [ ] **Step 2: Verify failure**

```bash
python3 -m pytest tests/summaries/test_snapshot.py -q
```

- [ ] **Step 3: Implement summary models and canonical hashing**

Define immutable `WeeklySnapshot`, `SnapshotTask`, `SnapshotStats`,
`StructuredSummary`, and `ArchiveItem`. Hash canonical UTF-8 JSON using
`sort_keys=True` and separators `(",", ":")`; exclude the `sha256` field
itself from the digest.

- [ ] **Step 4: Implement snapshot and local fallback**

Use `task_events` for created/completed/cancelled/changed sections, current
pending tasks for ongoing/overdue sections, and terminal standalone reminder
occurrences for reminder statistics. `LocalFallbackProvider.generate`
produces `StructuredSummary` from those facts without network access.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/summaries/test_snapshot.py -q
git add app/summaries tests/summaries/test_snapshot.py
git commit -m "feat: build deterministic weekly task snapshots"
```

## Task 13: Add DeepSeek structured summaries and Markdown rendering

**Files:**
- Create: `app/summaries/providers/deepseek.py`
- Create: `app/summaries/renderer.py`
- Create: `tests/summaries/test_deepseek.py`
- Create: `tests/summaries/test_renderer.py`

- [ ] **Step 1: Write provider and renderer tests**

Extend `tests/fakes.py` with:

```python
@dataclass(frozen=True)
class RecordedRequest:
    url: str
    headers: dict[str, str]
    json: dict[str, object]


class FakeHttpTransport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[RecordedRequest] = []

    def post(self, url: str, *, headers: dict[str, str],
             json_body: dict[str, object], timeout: int) -> dict[str, object]:
        self.requests.append(RecordedRequest(url, dict(headers), dict(json_body)))
        return self.response
```

Add `fake_http`, `weekly_snapshot`, and `structured_summary` fixtures to
`tests/conftest.py`; all fixture task titles are fictional.

```python
def test_deepseek_provider_requests_json_without_secrets(fake_http, weekly_snapshot) -> None:
    provider = DeepSeekSummaryProvider(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        api_key="secret-key",
        transport=fake_http,
    )
    result = provider.generate(weekly_snapshot)
    payload = fake_http.requests[0].json
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["model"] == "deepseek-v4-pro"
    assert "secret-key" not in json.dumps(payload, ensure_ascii=False)
    assert result.overview


def test_renderer_appends_authoritative_archive_manifest(weekly_snapshot, structured_summary) -> None:
    markdown = SummaryRenderer().render(weekly_snapshot, structured_summary)
    assert "## 系统归档清单" in markdown
    assert weekly_snapshot.archive_items[0].task_id[:8] in markdown
    assert f"snapshot-sha256: {weekly_snapshot.sha256}" in markdown
```

- [ ] **Step 2: Verify failures**

```bash
python3 -m pytest tests/summaries/test_deepseek.py tests/summaries/test_renderer.py -q
```

- [ ] **Step 3: Implement the provider**

POST to `<base_url>/chat/completions` with:

```json
{
  "model": "deepseek-v4-pro",
  "messages": [
    {"role": "system", "content": "根据给定 JSON 事实生成严格 JSON 周总结，不改变数量、状态或日期。"},
    {"role": "user", "content": "<canonical snapshot JSON>"}
  ],
  "response_format": {"type": "json_object"},
  "temperature": 0.2,
  "max_tokens": 3000
}
```

Validate every returned field and reject task claims not present in the
snapshot. One schema-repair request is allowed; a second invalid response
raises `SummaryProviderError`, which `SummaryService` catches and replaces
with `LocalFallbackProvider`.

- [ ] **Step 4: Implement deterministic Markdown**

The renderer writes model prose sections first, then local numeric statistics,
then one archive row per snapshot task. Escape Markdown table pipes and
newlines. Append:

```html
<!-- snapshot-sha256: <64 lowercase hex characters> -->
```

Use `atomic_write_text` for the draft.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/summaries/test_deepseek.py tests/summaries/test_renderer.py -q
git add app/summaries/providers/deepseek.py app/summaries/renderer.py \
  tests/summaries/test_deepseek.py tests/summaries/test_renderer.py \
  tests/fakes.py tests/conftest.py
git commit -m "feat: generate weekly summaries with DeepSeek"
```

## Task 14: Publish confirmed summaries and safely clean data

**Files:**
- Create: `app/integrations/__init__.py`
- Create: `app/integrations/git_publisher.py`
- Create: `app/summaries/service.py`
- Create: `tests/integrations/test_git_publisher.py`
- Create: `tests/summaries/test_service.py`

- [ ] **Step 1: Write exact-file Git publication tests**

```python
def test_publisher_stages_only_summary_file(private_repo_fixture, summary_file) -> None:
    publisher = GitPublisher(
        repo_path=private_repo_fixture.worktree,
        repo_slug="AngleBeatrowcolum/personal-weekly-summaries",
        visibility_checker=lambda slug: slug == "AngleBeatrowcolum/personal-weekly-summaries",
    )
    result = publisher.publish(summary_file, iso_year=2026, iso_week=30)
    changed = git(private_repo_fixture.worktree, "show", "--name-only", "--format=")
    assert changed.strip() == "summaries/2026/2026-W30.md"
    assert result.remote_commit_sha == private_repo_fixture.remote_head()
```

- [ ] **Step 2: Write cleanup safety tests**

```python
def test_cleanup_requires_published_unchanged_archive(summary_service, old_tasks) -> None:
    published, changed, unarchived = old_tasks
    summary_service.repository.mark_archive(published)
    summary_service.repository.mark_archive(changed)
    summary_service.tasks.update_title(changed.task_id, "修改后的任务")
    deleted = summary_service.cleanup(now=UTC_NOW)
    assert deleted.task_ids == [published.task_id]
    assert summary_service.tasks.get(changed.task_id) is not None
    assert summary_service.tasks.get(unarchived.task_id) is not None
```

- [ ] **Step 3: Verify failures**

```bash
python3 -m pytest tests/integrations/test_git_publisher.py tests/summaries/test_service.py -q
```

- [ ] **Step 4: Implement private publication**

Before any copy or Git mutation, `visibility_checker` must return true. The
default checker invokes:

```text
gh repo view <slug> --json visibility --jq .visibility
```

and requires `PRIVATE`. If `gh` is unavailable or authentication fails,
publication stops with a readable configuration error.

Copy to `summaries/<year>/<year>-W<week>.md`, run `git add -- <exact-path>`,
reject any other staged path, commit, push without force, and compare
`git rev-parse HEAD` with `git ls-remote origin refs/heads/<branch>`.

- [ ] **Step 5: Implement summary state and cleanup**

`SummaryService` transitions:

```text
pending -> generating -> awaiting_approval
awaiting_approval -> publishing -> published -> cleaned
```

`publish(run_id, confirmed=False)` raises `ConfirmationRequired` unless true.
After remote verification, create archive markers only when current
`tasks.updated_at` equals the snapshot value and the task is in the rendered
manifest.

Cleanup selects:

```sql
(status='completed' AND completed_at <= :cutoff)
OR (status='cancelled' AND cancelled_at <= :cutoff)
OR (status='pending' AND due_at <= :cutoff)
```

and also requires a valid archive marker linked to a published run and an
unchanged `updated_at`. Create a pre-clean backup, delete in one transaction,
run integrity check, create a post-clean backup, and retain the pre-clean
backup for at most 24 hours.

In the same cleanup transaction, remove terminal standalone reminder
occurrences older than 14 days only when their weekly sent/failed/skipped
statistics belong to a published summary. Keep the standalone weekly rule.

- [ ] **Step 6: Run tests and commit**

```bash
python3 -m pytest tests/integrations/test_git_publisher.py tests/summaries/test_service.py -q
git add app/integrations app/summaries/service.py tests/integrations \
  tests/summaries/test_service.py
git commit -m "feat: publish and archive confirmed weekly summaries"
```

## Task 15: Schedule weekly summaries and expose confirmation tools

**Files:**
- Create: `app/workers/weekly_summary_worker.py`
- Create: `app/platforms/weekly_summary_task.py`
- Modify: `app/agent/task_tools.py`
- Modify: `app/core/app_context.py`
- Modify: `app/core/bootstrap.py`
- Modify: `app/ui/pet_window.py`
- Create: `tests/workers/test_workers.py`
- Create: `tests/platforms/test_reminder_task.py`
- Create: `tests/agent/test_task_tools.py`

- [ ] **Step 1: Add failing weekly workflow tests**

```python
def test_weekly_worker_catches_up_after_sunday(summary_worker, shanghai_clock) -> None:
    shanghai_clock.set("2026-07-27 08:00:00")
    result = summary_worker.run_once()
    assert result.iso_week == 30
    assert result.status == "awaiting_approval"
    assert summary_worker.run_once().run_id == result.run_id


def test_summary_publish_tool_requires_confirmation(tool_registry) -> None:
    result = tool_registry.prepare_or_execute(
        "weekly_summary_publish", {"run_id": "run-1"}
    )
    assert isinstance(result, PendingToolAction)
```

- [ ] **Step 2: Verify failures**

```bash
python3 -m pytest tests/workers/test_workers.py tests/platforms/test_reminder_task.py \
  tests/agent/test_task_tools.py -q
```

- [ ] **Step 3: Implement weekly worker and Task Scheduler XML**

The worker checks the most recent due ISO week, creates a snapshot, tries
DeepSeek only when enabled and a key exists, otherwise uses local fallback,
writes the draft, and exits. It never publishes.

The scheduled task has a Sunday 20:00 calendar trigger plus logon trigger,
`StartWhenAvailable=true`, battery execution enabled, and `WakeToRun=false`.

- [ ] **Step 4: Implement summary tools and desktop notification**

Register:

```text
weekly_summary_get
weekly_summary_regenerate
weekly_summary_publish
```

`weekly_summary_publish` is `requires_confirmation=True`, `risk="high"`, and
calls `SummaryService.publish(run_id, confirmed=True)` only after the existing
pending-tool UI confirms it. Sakura polls for newly generated
`awaiting_approval` runs and displays a deterministic bubble/TTS summary once.

- [ ] **Step 5: Run tests and commit checkpoint three**

```bash
python3 -m pytest tests/workers tests/platforms tests/agent \
  tests/summaries tests/integrations -q
git add app/workers/weekly_summary_worker.py app/platforms/weekly_summary_task.py \
  app/agent/task_tools.py app/core app/ui/pet_window.py tests
git commit -m "feat: schedule and confirm weekly summaries"
```

## Task 16: Add setup documentation and end-to-end verification

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`
- Create: `tests/test_task_assistant_e2e.py`

- [ ] **Step 1: Write an end-to-end local workflow test**

```python
def test_task_assistant_local_workflow(tmp_path, fixed_now, fake_mailer, fake_summary_provider) -> None:
    app = build_test_task_assistant(
        tmp_path,
        now=fixed_now,
        mailer=fake_mailer,
        summary_provider=fake_summary_provider,
    )
    task = app.tasks.create_task(
        title="实验报告",
        priority="high",
        planned_date="2026-07-25",
        due_at="2026-07-26T04:00:00Z",
        now=fixed_now,
    )
    assert app.tasks.query_today(now=fixed_now).summary.high_priority == 1
    app.reminders.run_email_once(now=fixed_now + timedelta(days=1))
    assert len(fake_mailer.messages) == 1
    run = app.summaries.generate_due(now=fixed_now + timedelta(days=1))
    assert run.status == "awaiting_approval"
    assert app.tasks.get_task(task.id) is not None
```

- [ ] **Step 2: Verify the test fails, then add the test composition helper**

```bash
python3 -m pytest tests/test_task_assistant_e2e.py -q
```

Expected: failure because `build_test_task_assistant` is not defined. Add the
helper to `tests/conftest.py` by composing the real database, repositories,
services, fake external adapters, and injected clock; do not duplicate
production logic.

- [ ] **Step 3: Document setup and recovery**

README must include exact commands:

```bat
install.bat
setup-task-assistant.bat
start.bat
```

Document QQ SMTP authorization codes, DeepSeek key storage, private summary
repository prerequisites, `gh auth login`, Windows scheduled task names,
backup location, seven-file/200-MB limits, explicit restore confirmation, and
how to disable the two scheduled tasks.

- [ ] **Step 4: Run the full automated verification**

```bash
python3 -m compileall -q app main.py tools
QT_QPA_PLATFORM=offscreen python3 -m pytest -q
git diff --check
```

Expected: compile succeeds, all tests pass, and no whitespace errors are
reported.

- [ ] **Step 5: Run Windows smoke checks**

From Windows PowerShell:

```powershell
.\runtime\python.exe -m pip install -r .\requirements.txt
.\runtime\python.exe -m app.workers.reminder_worker --base-dir .
schtasks /Query /TN "\Sakura\TaskReminderWorker" /V /FO LIST
schtasks /Query /TN "\Sakura\WeeklySummaryWorker" /V /FO LIST
```

Expected: dependency installation succeeds, an empty reminder run exits
cleanly, both tasks exist, battery execution is allowed, and wake-to-run is
disabled.

- [ ] **Step 6: Commit documentation and final verification state**

```bash
git add README.md .gitignore tests/test_task_assistant_e2e.py tests/conftest.py
git commit -m "docs: add task assistant setup and recovery guide"
git status --short --branch
```

Expected: only deliberately uncommitted local runtime/data files are absent
because they are ignored; the branch contains no unintended source changes.

## Final acceptance run

After all task commits:

```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest -q
python3 -m compileall -q app main.py tools
git diff --check HEAD~16..HEAD
git status --short --branch
```

Then perform the manual acceptance scenarios from design section 20 using
fictional task data. Do not send a real QQ email, call DeepSeek with real
personal tasks, push the summary repository, or clean real task data until
the user has completed local credential setup and explicitly approves those
external actions.
