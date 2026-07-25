from app.notifications.qq_mail import QQMailer
from tests.fakes import FakeCredentialStore, FakeSMTP


def test_qq_mailer_uses_ssl_and_keeps_secret_out_of_errors() -> None:
    fake_smtp = FakeSMTP()
    mailer = QQMailer(
        host="smtp.qq.com",
        port=465,
        sender="123456@qq.com",
        recipient="123456@qq.com",
        credential_store=FakeCredentialStore({"qq-smtp": "secret-auth-code"}),
        smtp_factory=lambda *_args, **_kwargs: fake_smtp,
    )

    mailer.send(subject="任务提醒", body="实验报告将在两小时后截止。")

    assert fake_smtp.login_calls == [("123456@qq.com", "secret-auth-code")]
    assert fake_smtp.sent_messages[0]["Subject"] == "任务提醒"
