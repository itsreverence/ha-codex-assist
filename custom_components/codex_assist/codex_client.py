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


@dataclass(frozen=True)
class CodexToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class CodexTurnResult:
    text: str
    tool_calls: list[CodexToolCall]


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
        result = await self.generate_turn(
            model=model,
            instructions=instructions,
            input_items=codex_messages_to_input_items(messages),
        )
        return result.text

    async def generate_turn(
        self,
        *,
        model: str,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> CodexTurnResult:
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        response = await self._http_client.post(
            f"{self._base_url}/responses",
            headers=codex_headers(self._access_token),
            json=payload,
        )
        if response.status_code != 200:
            detail = _response_error_detail(response)
            raise RuntimeError(
                f"Codex request failed with status {response.status_code}: {detail}"
            )
        if response.text:
            return extract_streamed_turn_result(response.text)
        payload = response.json()
        return CodexTurnResult(
            text=extract_output_text(payload),
            tool_calls=extract_tool_calls(payload),
        )


def codex_messages_to_input_items(messages: list[CodexMessage]) -> list[dict[str, Any]]:
    return [{"role": message.role, "content": message.content} for message in messages]


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
    return extract_streamed_turn_result(stream_text).text


def extract_streamed_turn_result(stream_text: str) -> CodexTurnResult:
    delta_parts: list[str] = []
    done_parts: list[str] = []
    tool_calls: list[CodexToolCall] = []
    current_tool_call: dict[str, Any] | None = None
    current_arguments = ""

    for event in _iter_sse_events(stream_text):
        event_type = event.get("type")
        if event_type == "error":
            raise RuntimeError(_event_error_detail(event))
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                delta_parts.append(delta)
            continue
        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                current_tool_call = item
                current_arguments = str(item.get("arguments") or "")
            continue
        if event_type == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                current_arguments += delta
            continue
        if event_type == "response.function_call_arguments.done":
            arguments = event.get("arguments")
            if isinstance(arguments, str):
                current_arguments = arguments
            if current_tool_call is not None:
                tool_calls.append(_tool_call_from_item(current_tool_call, current_arguments))
                current_tool_call = None
                current_arguments = ""
            continue
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                if item.get("type") == "function_call":
                    tool_calls.append(
                        _tool_call_from_item(item, str(item.get("arguments") or ""))
                    )
                else:
                    done_parts.append(extract_output_text({"output": [item]}))

    # Codex streams both text deltas and the completed message item. The
    # completed item repeats the same visible text, so prefer deltas when
    # present and only fall back to output_item.done when no deltas arrived.
    parts = delta_parts or done_parts
    return CodexTurnResult(text="".join(parts).strip(), tool_calls=tool_calls)


def _iter_sse_events(stream_text: str):
    data_lines: list[str] = []

    def flush_event():
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        data_lines.clear()
        if data == "[DONE]":
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    for line in stream_text.splitlines():
        if not line.strip():
            payload = flush_event()
            if payload is not None:
                yield payload
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())

    payload = flush_event()
    if payload is not None:
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


def extract_tool_calls(payload: dict[str, Any]) -> list[CodexToolCall]:
    tool_calls: list[CodexToolCall] = []
    for item in payload.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            tool_calls.append(_tool_call_from_item(item, str(item.get("arguments") or "")))
    return tool_calls


def _tool_call_from_item(item: dict[str, Any], arguments: str) -> CodexToolCall:
    parsed_arguments: dict[str, Any] = {}
    if arguments:
        try:
            loaded = json.loads(arguments)
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            parsed_arguments = loaded

    call_id = item.get("call_id") or item.get("id") or ""
    name = item.get("name") or ""
    return CodexToolCall(
        id=str(call_id),
        name=str(name),
        arguments=parsed_arguments,
    )
