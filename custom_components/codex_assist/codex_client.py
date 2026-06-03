from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol

CODEX_BACKEND_BASE_URL = "https://chatgpt.com/backend-api/codex"


class AsyncPostClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class CodexMessage:
    role: str
    content: str


class CodexClient:
    def __init__(
        self,
        *,
        http_client: AsyncPostClient,
        access_token: str,
        base_url: str = CODEX_BACKEND_BASE_URL,
    ) -> None:
        self._http_client = http_client
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")

    async def generate_text(
        self,
        *,
        model: str,
        instructions: str,
        messages: list[CodexMessage],
    ) -> str:
        response = await self._http_client.post(
            f"{self._base_url}/responses",
            headers=codex_headers(self._access_token),
            json={
                "model": model,
                "instructions": instructions,
                "input": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                "store": False,
                "stream": True,
            },
        )
        if response.status_code != 200:
            detail = _response_error_detail(response)
            raise RuntimeError(
                f"Codex request failed with status {response.status_code}: {detail}"
            )
        if response.text:
            return extract_streamed_output_text(response.text)
        return extract_output_text(response.json())


def codex_headers(access_token: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "codex_cli_rs/0.0.0 (Codex Assist)",
        "originator": "codex_cli_rs",
    }
    account_id = _chatgpt_account_id(access_token)
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def _chatgpt_account_id(access_token: str) -> str | None:
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        account_id = claims.get("https://api.openai.com/auth", {}).get(
            "chatgpt_account_id"
        )
    except Exception:
        return None
    return account_id if isinstance(account_id, str) and account_id else None


def extract_streamed_output_text(stream_text: str) -> str:
    parts: list[str] = []
    for event in _iter_sse_events(stream_text):
        event_type = event.get("type")
        if event_type == "error":
            raise RuntimeError(_event_error_detail(event))
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                parts.append(delta)
            continue
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                parts.append(extract_output_text({"output": [item]}))
    return "".join(parts).strip()


def _iter_sse_events(stream_text: str):
    for block in stream_text.split("\n\n"):
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _response_error_detail(response: Any) -> str:
    text = getattr(response, "text", "") or ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:500] if text else "unknown error"
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload.get("error")
        if isinstance(detail, str):
            return detail
    return text[:500] if text else "unknown error"


def _event_error_detail(event: dict[str, Any]) -> str:
    error = event.get("error") or event.get("message") or event.get("detail")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or error.get("code")
        if isinstance(message, str):
            return message
    return "Codex stream returned an error event"


def extract_output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts).strip()
