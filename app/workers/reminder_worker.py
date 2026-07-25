"""Windows 计划任务调用的轻量邮件提醒 worker。"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.core.runtime_log import log_event
from app.notifications.credentials import KeyringCredentialStore
from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.qq_mail import QQMailer
from app.storage.paths import StoragePaths
from app.tasks.backup import DatabaseBackupService
from app.tasks.database import TaskDatabase
from app.tasks.migration import LegacyJsonMigrator
from app.tasks.repository import ReminderRepository
from app.tasks.scheduler import ReminderScheduler
from app.tasks.settings import TaskAssistantSettings


@dataclass(frozen=True)
class WorkerResult:
    sent: int = 0
    failed: int = 0
    skipped: bool = False


def run_worker(base_dir: Path) -> WorkerResult:
    """检查一次到期邮件提醒后退出；不导入窗口、TTS 或模型。"""

    paths = StoragePaths(base_dir)
    database = TaskDatabase(paths.tasks_database())
    database.initialize()
    LegacyJsonMigrator(database, legacy_data_dir=paths.data_dir).run()
    DatabaseBackupService(database, paths.task_database_backup_dir).create(reason="daily")

    settings = TaskAssistantSettings.load(paths.task_assistant_config())
    if not settings.email_enabled:
        return WorkerResult(skipped=True)
    if not settings.qq_email or not settings.recipient_email:
        raise ValueError("已启用邮件提醒，但 QQ 发件或收件邮箱未配置。")

    scheduler = ReminderScheduler(ReminderRepository(database))
    mailer = QQMailer(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.qq_email,
        recipient=settings.recipient_email,
        credential_store=KeyringCredentialStore(),
    )
    result = NotificationDispatcher(scheduler, mailer).dispatch_email()
    return WorkerResult(sent=result["sent"], failed=result["failed"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行一次 Sakura QQ 邮件提醒检查。")
    parser.add_argument(
        "--base-dir", type=Path, default=_PROJECT_ROOT
    )
    arguments = parser.parse_args(argv)
    try:
        result = run_worker(arguments.base_dir)
    except Exception as exc:  # noqa: BLE001 - worker must return a useful exit code
        log_event("ReminderWorker", "邮件提醒检查失败", {"error": str(exc)})
        return 1
    log_event(
        "ReminderWorker",
        "邮件提醒检查完成",
        {"sent": result.sent, "failed": result.failed, "skipped": result.skipped},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
