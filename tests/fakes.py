from __future__ import annotations

from dataclasses import dataclass


class FakeCredentialStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def __repr__(self) -> str:
        return "FakeCredentialStore(<redacted>)"


class FakeSMTP:
    def __init__(self, *_args, **_kwargs) -> None:
        self.login_calls: list[tuple[str, str]] = []
        self.sent_messages: list[object] = []

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        self.login_calls.append((username, password))

    def send_message(self, message: object) -> None:
        self.sent_messages.append(message)


@dataclass(frozen=True)
class RecordedRequest:
    url: str
    headers: dict[str, str]
    json: dict[str, object]


class FakeHttpTransport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[RecordedRequest] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, object],
        timeout: int,
    ) -> dict[str, object]:
        self.requests.append(RecordedRequest(url, dict(headers), dict(json_body)))
        return self.response
