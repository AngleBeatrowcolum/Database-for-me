"""手动配置 Sakura 任务邮件提醒。"""

from __future__ import annotations

import argparse
import getpass
import sys
from dataclasses import replace
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.notifications.credentials import KeyringCredentialStore, QQ_SMTP_CREDENTIAL
from app.platforms.reminder_task import register_reminder_task, write_reminder_task_xml
from app.platforms.weekly_summary_task import (
    register_weekly_summary_task,
    build_weekly_summary_task_xml,
)
from app.storage.paths import StoragePaths
from app.tasks.backup import DatabaseBackupService
from app.tasks.database import TaskDatabase
from app.tasks.settings import TaskAssistantSettings


def _configure(base_dir: Path) -> None:
    paths = StoragePaths(base_dir)
    current = TaskAssistantSettings.load(paths.task_assistant_config())
    sender = input(f"QQ 发件邮箱 [{current.qq_email}]: ").strip() or current.qq_email
    recipient = input(f"收件邮箱 [{current.recipient_email or sender}]: ").strip() or current.recipient_email or sender
    if not sender or not recipient:
        raise ValueError("发件邮箱和收件邮箱不能为空。")
    authorization_code = getpass.getpass("QQ SMTP 授权码（不会显示或写入配置文件）: ")
    if not authorization_code:
        raise ValueError("QQ SMTP 授权码不能为空。")
    KeyringCredentialStore().set(QQ_SMTP_CREDENTIAL, authorization_code)
    replace(current, email_enabled=True, qq_email=sender, recipient_email=recipient).save(
        paths.task_assistant_config()
    )
    database = TaskDatabase(paths.tasks_database())
    database.initialize()
    DatabaseBackupService(database, paths.task_database_backup_dir).create(reason="manual")
    print("已保存非秘密邮件设置，并将授权码存入 Windows 凭据管理器。")


def _register(base_dir: Path) -> None:
    config_dir = StoragePaths(base_dir).config_dir
    xml_path = config_dir / "sakura-task-reminder-worker.xml"
    write_reminder_task_xml(
        xml_path,
        python_exe=base_dir / "runtime" / "python.exe",
        worker_path=base_dir / "app" / "workers" / "reminder_worker.py",
        base_dir=base_dir,
    )
    register_reminder_task(xml_path)
    weekly_xml = config_dir / "sakura-weekly-summary-worker.xml"
    weekly_xml.write_text(
        build_weekly_summary_task_xml(
            python_exe=base_dir / "runtime" / "python.exe",
            worker_path=base_dir / "app" / "workers" / "weekly_summary_worker.py",
            base_dir=base_dir,
        ),
        encoding="utf-16",
    )
    register_weekly_summary_task(weekly_xml)
    print("已注册 Sakura 邮件提醒和周总结计划任务。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配置 Sakura 任务提醒。")
    parser.add_argument("command", choices=("configure", "register-tasks"))
    parser.add_argument("--base-dir", type=Path, default=_PROJECT_ROOT)
    arguments = parser.parse_args(argv)
    # Windows 的命令行解析会把带尾部反斜杠的引号参数误读为字面量双引号。
    # 兼容旧批处理脚本和手动输入，不让它写入错误目录。
    base_dir = Path(str(arguments.base_dir).rstrip('"'))
    if arguments.command == "configure":
        _configure(base_dir)
    else:
        _register(base_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
