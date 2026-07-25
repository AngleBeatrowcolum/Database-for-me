"""QQ SMTP SSL 发送与不含秘密的错误分类。"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Callable, Protocol

from app.notifications.credentials import CredentialStore, QQ_SMTP_CREDENTIAL


class SMTPClient(Protocol):
    def __enter__(self) -> "SMTPClient": ...

    def __exit__(self, *_args: object) -> None: ...

    def login(self, username: str, password: str) -> object: ...

    def send_message(self, message: EmailMessage) -> object: ...


SMTPFactory = Callable[..., SMTPClient]


class QQMailDeliveryError(RuntimeError):
    """可由调度器处理的脱敏邮件投递错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class QQMailer:
    """通过 QQ SMTP SSL 发送单封提醒。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        recipient: str,
        credential_store: CredentialStore,
        smtp_factory: SMTPFactory = smtplib.SMTP_SSL,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.recipient = recipient
        self._credentials = credential_store
        self._smtp_factory = smtp_factory

    def send(self, *, subject: str, body: str) -> None:
        authorization_code = self._credentials.get(QQ_SMTP_CREDENTIAL)
        if not authorization_code:
            raise QQMailDeliveryError("auth", "未配置 QQ SMTP 授权码。")
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(body)
        try:
            with self._smtp_factory(self.host, self.port, timeout=20) as smtp:
                smtp.login(self.sender, authorization_code)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise QQMailDeliveryError("auth", "QQ SMTP 身份验证失败。") from exc
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise QQMailDeliveryError("permanent", "QQ 邮件收件人地址无效。") from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise QQMailDeliveryError("temporary", "QQ SMTP 暂时不可用。") from exc
