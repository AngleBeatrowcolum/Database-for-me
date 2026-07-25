"""Windows 凭据管理器的最小适配层；绝不把秘密写入配置或日志。"""

from __future__ import annotations

import os
from typing import Protocol


SERVICE_NAME = "sakura-task-assistant"
QQ_SMTP_CREDENTIAL = "qq-smtp"
DEEPSEEK_API_CREDENTIAL = "deepseek-api"


class CredentialStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...


class CredentialStoreError(RuntimeError):
    """凭据库不可用；错误信息不包含秘密。"""


class KeyringCredentialStore:
    """基于 keyring 的凭据存储，服务名固定且 repr 脱敏。"""

    def __init__(self, keyring_module: object | None = None) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module  # type: ignore[no-redef]
            except ImportError as exc:
                raise CredentialStoreError("未安装系统凭据库支持。") from exc
        self._keyring = keyring_module
        if os.name == "nt":
            backend = self._keyring.get_keyring()
            priority = getattr(backend, "priority", 0)
            priority = priority() if callable(priority) else priority
            if not isinstance(priority, (int, float)) or priority <= 0:
                raise CredentialStoreError("Windows 凭据库后端不可用。")

    def get(self, name: str) -> str | None:
        try:
            return self._keyring.get_password(SERVICE_NAME, _credential_name(name))
        except Exception as exc:  # noqa: BLE001 - third-party backend errors vary
            raise CredentialStoreError("读取凭据失败。") from exc

    def set(self, name: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("凭据不能为空。")
        try:
            self._keyring.set_password(SERVICE_NAME, _credential_name(name), value)
        except Exception as exc:  # noqa: BLE001 - third-party backend errors vary
            raise CredentialStoreError("保存凭据失败。") from exc

    def __repr__(self) -> str:
        return "KeyringCredentialStore(<redacted>)"


def _credential_name(name: str) -> str:
    if name not in {QQ_SMTP_CREDENTIAL, DEEPSEEK_API_CREDENTIAL}:
        raise ValueError("不支持的凭据名称。")
    return name
