"""受限的 DeepSeek 周总结提供者。"""

from __future__ import annotations

import json
from typing import Protocol
from urllib.request import Request, urlopen

from app.summaries.models import StructuredSummary, WeeklySnapshot


class SummaryProviderError(RuntimeError):
    """模型响应无法安全地转换为结构化周总结。"""


class HttpTransport(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json_body: dict[str, object], timeout: int) -> dict[str, object]: ...


class UrllibHttpTransport:
    def post(self, url: str, *, headers: dict[str, str], json_body: dict[str, object], timeout: int) -> dict[str, object]:
        request = Request(url, data=json.dumps(json_body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured HTTPS endpoint
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise SummaryProviderError("DeepSeek 返回的响应不是对象。")
        return payload


class DeepSeekSummaryProvider:
    """只将白名单事实快照发送给 DeepSeek，且只接受可验证的任务标题。"""

    def __init__(self, *, base_url: str, model: str, api_key: str, transport: HttpTransport | None = None, timeout_seconds: int = 30) -> None:
        if not api_key:
            raise ValueError("DeepSeek API Key 不能为空。")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._transport = transport or UrllibHttpTransport()
        self._timeout_seconds = timeout_seconds

    def generate(self, snapshot: WeeklySnapshot) -> StructuredSummary:
        content = self._request(snapshot)
        try:
            return self._parse_and_validate(content, snapshot)
        except SummaryProviderError:
            return self._parse_and_validate(self._request(snapshot, repair="上一次输出不符合 JSON schema 或包含不存在的任务。只返回合法 JSON。"), snapshot)

    def _request(self, snapshot: WeeklySnapshot, repair: str | None = None) -> object:
        messages = [
            {"role": "system", "content": "根据给定 JSON 事实生成严格 JSON 周总结，不改变数量、状态或日期。"},
            {"role": "user", "content": json.dumps(snapshot.canonical_payload(), ensure_ascii=False, sort_keys=True)},
        ]
        if repair:
            messages.append({"role": "user", "content": repair})
        response = self._transport.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json_body={"model": self._model, "messages": messages, "response_format": {"type": "json_object"}, "temperature": 0.2, "max_tokens": 3000},
            timeout=self._timeout_seconds,
        )
        try:
            content = response["choices"][0]["message"]["content"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise SummaryProviderError("DeepSeek 响应缺少 choices.message.content。") from exc
        if not isinstance(content, str):
            raise SummaryProviderError("DeepSeek 响应内容不是文本。")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise SummaryProviderError("DeepSeek 未返回 JSON。") from exc

    @staticmethod
    def _parse_and_validate(content: object, snapshot: WeeklySnapshot) -> StructuredSummary:
        if not isinstance(content, dict):
            raise SummaryProviderError("总结 JSON 必须是对象。")
        fields = ("overview", "completed_items", "ongoing_items", "overdue_items", "next_focus")
        if set(content) != set(fields) or not isinstance(content.get("overview"), str):
            raise SummaryProviderError("总结 JSON 字段不完整。")
        allowed_titles = {item.title for group in (snapshot.completed, snapshot.ongoing, snapshot.overdue) for item in group}
        parsed: dict[str, tuple[str, ...]] = {}
        for field in fields[1:]:
            value = content[field]
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise SummaryProviderError(f"{field} 必须是字符串列表。")
            if any(item not in allowed_titles for item in value):
                raise SummaryProviderError(f"{field} 包含不在任务快照中的项目。")
            parsed[field] = tuple(value)
        return StructuredSummary(overview=content["overview"], completed_items=parsed["completed_items"], ongoing_items=parsed["ongoing_items"], overdue_items=parsed["overdue_items"], next_focus=parsed["next_focus"])
