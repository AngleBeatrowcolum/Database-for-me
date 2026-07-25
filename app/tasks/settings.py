"""任务助手的非秘密 JSON 设置。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.storage.atomic import atomic_write_text


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

    def to_dict(self) -> dict[str, Any]:
        """返回可安全落盘的设置，不包含任何凭据。"""

        return {
            "timezone": self.timezone,
            "email_enabled": self.email_enabled,
            "qq_email": self.qq_email,
            "recipient_email": self.recipient_email,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "summary_enabled": self.summary_enabled,
            "summary_repo_path": self.summary_repo_path,
            "summary_repo_slug": self.summary_repo_slug,
            "deepseek_enabled": self.deepseek_enabled,
            "deepseek_base_url": self.deepseek_base_url,
            "deepseek_model": self.deepseek_model,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "TaskAssistantSettings":
        """从非秘密映射恢复设置，字段错误时回退到安全默认值。"""

        defaults = cls()
        return cls(
            timezone=_string_value(data, "timezone", defaults.timezone),
            email_enabled=_bool_value(data, "email_enabled", defaults.email_enabled),
            qq_email=_string_value(data, "qq_email", defaults.qq_email),
            recipient_email=_string_value(data, "recipient_email", defaults.recipient_email),
            smtp_host=_string_value(data, "smtp_host", defaults.smtp_host),
            smtp_port=_int_value(data, "smtp_port", defaults.smtp_port),
            summary_enabled=_bool_value(data, "summary_enabled", defaults.summary_enabled),
            summary_repo_path=_string_value(data, "summary_repo_path", defaults.summary_repo_path),
            summary_repo_slug=_string_value(data, "summary_repo_slug", defaults.summary_repo_slug),
            deepseek_enabled=_bool_value(data, "deepseek_enabled", defaults.deepseek_enabled),
            deepseek_base_url=_string_value(data, "deepseek_base_url", defaults.deepseek_base_url),
            deepseek_model=_string_value(data, "deepseek_model", defaults.deepseek_model),
        )

    @classmethod
    def load(cls, path: Path) -> "TaskAssistantSettings":
        """加载设置；缺失或损坏的 JSON 使用默认值。"""

        path = Path(path)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls.from_dict(data)

    def save(self, path: Path) -> None:
        """原子保存非秘密 JSON 设置。"""

        atomic_write_text(
            Path(path),
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            backup=True,
        )


def _string_value(data: Mapping[str, object], key: str, default: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else default


def _bool_value(data: Mapping[str, object], key: str, default: bool) -> bool:
    value = data.get(key)
    return value if isinstance(value, bool) else default


def _int_value(data: Mapping[str, object], key: str, default: int) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default
