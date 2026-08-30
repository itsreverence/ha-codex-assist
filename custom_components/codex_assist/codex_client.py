from __future__ import annotations

import base64
import copy
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from .codex_image import image_model_quality, validate_image_size

CODEX_BACKEND_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_STREAM_TIMEOUT = 300


class AsyncPostClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...

    def stream(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class CodexMessage:
    role: str
    content: str | list[dict[str, Any]]


@dataclass(frozen=True)
class CodexToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class CodexTurnResult:
    text: str
    tool_calls: list[CodexToolCall]


@dataclass(frozen=True)
class CodexImageResult:
    image_data: bytes
    mime_type: str
    model: str
    revised_prompt: str | None = None


@dataclass(frozen=True)
class CodexTextDelta:
    text: str


@dataclass(frozen=True)
class CodexToolCallDelta:
    tool_call: CodexToolCall


@dataclass(frozen=True)
class CodexCitation:
    title: str
    url: str
    start_index: int | None = None
    end_index: int | None = None


@dataclass(frozen=True)
class CodexCitationDelta:
    citation: CodexCitation


@dataclass(frozen=True)
class CodexResponseItemDelta:
    item: dict[str, Any]


CodexStreamDelta = (
    CodexTextDelta | CodexToolCallDelta | CodexCitationDelta | CodexResponseItemDelta
)


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
        payload = _responses_payload(
            model=model,
            instructions=instructions,
            input_items=input_items,
            tools=tools,
        )

        response = await self._http_client.post(
            f"{self._base_url}/responses",
            headers=codex_headers(self._access_token),
            json=payload,
        )
        if response.status_code != 200:
            error = _response_error(response)
            if response.status_code == 401 or error.code == "token_invalidated":
                raise CodexAuthenticationError(f"Codex authentication failed: {error.detail}")
            if _is_rate_limit_response(response.status_code, error):
                raise CodexRateLimitError(
                    f"Codex usage limit or rate limit reached: {error.detail}"
                )
            raise RuntimeError(
                f"Codex request failed with status {response.status_code}: {error.detail}"
            )
        if response.text:
            return extract_streamed_turn_result(response.text)
        payload = response.json()
        return CodexTurnResult(
            text=extract_output_text(payload),
            tool_calls=extract_tool_calls(payload),
        )

    async def stream_turn(
        self,
        *,
        model: str,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
        reasoning_summary: str | None = None,
        text_verbosity: str | None = None,
        text_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[CodexStreamDelta]:
        payload = _responses_payload(
            model=model,
            instructions=instructions,
            input_items=input_items,
            tools=tools,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            text_verbosity=text_verbosity,
            text_format=text_format,
        )

        async with self._http_client.stream(
            "POST",
            f"{self._base_url}/responses",
            headers=codex_headers(self._access_token),
            json=payload,
            timeout=CODEX_STREAM_TIMEOUT,
        ) as response:
            if response.status_code != 200:
                error = await _stream_response_error(response)
                if response.status_code == 401 or error.code == "token_invalidated":
                    raise CodexAuthenticationError(f"Codex authentication failed: {error.detail}")
                if _is_rate_limit_response(response.status_code, error):
                    raise CodexRateLimitError(
                        f"Codex usage limit or rate limit reached: {error.detail}"
                    )
                raise RuntimeError(
                    f"Codex request failed with status {response.status_code}: {error.detail}"
                )

            pending_tool_calls: dict[str, tuple[dict[str, Any], str]] = {}
            completed_tool_call_ids: set[str] = set()
            anonymous_call_index = 0
            async for event in _aiter_sse_events(response):
                event_type = event.get("type")
                for citation in _citations_from_event(event):
                    yield CodexCitationDelta(citation)
                if event_type == "response.output_item.added":
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        key = _function_call_item_key(item, event)
                        if key is None:
                            anonymous_call_index += 1
                            key = f"anonymous-{anonymous_call_index}"
                        pending_tool_calls[key] = (item, str(item.get("arguments") or ""))
                    continue
                if event_type == "response.function_call_arguments.delta":
                    key = _function_call_event_key(event, pending_tool_calls)
                    delta = event.get("delta")
                    if key is not None and isinstance(delta, str):
                        item, arguments = pending_tool_calls[key]
                        pending_tool_calls[key] = (item, arguments + delta)
                    continue
                if event_type == "response.function_call_arguments.done":
                    key = _function_call_event_key(event, pending_tool_calls)
                    if key is not None:
                        pending_tool_call, pending_arguments = pending_tool_calls.pop(key)
                        arguments = event.get("arguments")
                        if isinstance(arguments, str):
                            pending_arguments = arguments
                        tool_call = _tool_call_from_item(pending_tool_call, pending_arguments)
                        completed_tool_call_ids.add(tool_call.id)
                        yield CodexToolCallDelta(tool_call)
                    continue
                if event_type == "response.output_item.done":
                    item = event.get("item")
                    if isinstance(item, dict):
                        yield CodexResponseItemDelta(copy.deepcopy(item))
                    call_id = ""
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        call_id = str(item.get("call_id") or item.get("id") or "")
                        key = _function_call_item_key(item, event)
                        if key is not None:
                            pending_tool_calls.pop(key, None)
                    if call_id and call_id in completed_tool_call_ids:
                        continue
                delta = _stream_delta_from_event(event)
                if delta is not None:
                    yield delta

    async def generate_image(
        self,
        *,
        prompt: str,
        input_items: list[dict[str, Any]] | None = None,
        chat_model: str = "gpt-5.4",
        image_model: str = "gpt-image-2-medium",
        size: str = "1024x1024",
    ) -> CodexImageResult:
        """Generate an image through Codex Responses image_generation tool."""
        payload = _image_generation_payload(
            prompt=prompt,
            input_items=input_items,
            chat_model=chat_model,
            image_model=image_model,
            size=size,
        )

        image_b64: str | None = None
        text_parts: list[str] = []
        async with self._http_client.stream(
            "POST",
            f"{self._base_url}/responses",
            headers=codex_headers(self._access_token),
            json=payload,
            timeout=300,
        ) as response:
            if response.status_code != 200:
                error = await _stream_response_error(response)
                if response.status_code == 401 or error.code == "token_invalidated":
                    raise CodexAuthenticationError(f"Codex authentication failed: {error.detail}")
                if _is_rate_limit_response(response.status_code, error):
                    raise CodexRateLimitError(
                        f"Codex usage limit or rate limit reached: {error.detail}"
                    )
                raise RuntimeError(
                    f"Codex image request failed with status {response.status_code}: {error.detail}"
                )

            async for event in _aiter_sse_events(response):
                found = _extract_image_b64(event)
                if found:
                    image_b64 = found
                delta = _stream_delta_from_event(event)
                if isinstance(delta, CodexTextDelta):
                    text_parts.append(delta.text)

        if not image_b64:
            raise RuntimeError("Codex response contained no image_generation result")

        try:
            image_data = base64.b64decode(image_b64, validate=True)
        except (ValueError, TypeError) as err:
            raise RuntimeError("Codex image_generation result was not valid base64") from err

        return CodexImageResult(
            image_data=image_data,
            mime_type="image/png",
            model=image_model,
            revised_prompt="".join(text_parts).strip() or None,
        )


def _responses_payload(
    *,
    model: str,
    instructions: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    reasoning_effort: str | None = None,
    reasoning_summary: str | None = None,
    text_verbosity: str | None = None,
    text_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "store": False,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    include: list[str] = []
    if any(tool.get("type") == "web_search" for tool in tools or []):
        include.append("web_search_call.action.sources")
    text_options: dict[str, Any] = {}
    if _supports_reasoning_options(model):
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
            if reasoning_summary and reasoning_summary != "off":
                payload["reasoning"]["summary"] = reasoning_summary
            include.insert(0, "reasoning.encrypted_content")
        if text_verbosity:
            text_options["verbosity"] = text_verbosity
    if text_format:
        text_options["format"] = text_format
    if text_options:
        payload["text"] = text_options
    if include:
        payload["include"] = include
    return payload


def _image_generation_payload(
    *,
    prompt: str,
    input_items: list[dict[str, Any]] | None,
    chat_model: str,
    image_model: str,
    size: str,
) -> dict[str, Any]:
    quality = image_model_quality(image_model)
    size = validate_image_size(size)
    return {
        "model": chat_model,
        "store": False,
        "instructions": (
            "You are an assistant that must fulfill image generation requests "
            "by using the image_generation tool when provided."
        ),
        "input": input_items
        or [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "tools": [
            {
                "type": "image_generation",
                "model": "gpt-image-2",
                "size": size,
                "quality": quality,
                "output_format": "png",
                "background": "opaque",
                "partial_images": 1,
            }
        ],
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "image_generation"}],
        },
        "stream": True,
    }


def _extract_image_b64(value: Any) -> str | None:
    """Return the newest image b64 embedded in a Responses event payload."""
    found: str | None = None
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call":
            result = value.get("result")
            if isinstance(result, str) and result:
                found = result
        partial = value.get("partial_image_b64")
        if isinstance(partial, str) and partial:
            found = partial
        for child in value.values():
            nested = _extract_image_b64(child)
            if nested:
                found = nested
    elif isinstance(value, list):
        for child in value:
            nested = _extract_image_b64(child)
            if nested:
                found = nested
    return found


def _supports_reasoning_options(model: str) -> bool:
    return model.startswith(("gpt-5", "o"))


def codex_messages_to_input_items(messages: list[CodexMessage]) -> list[dict[str, Any]]:
    return [{"role": message.role, "content": message.content} for message in messages]


def codex_user_content_with_images(
    text: str,
    images: list[tuple[str, bytes]],
) -> str | list[dict[str, Any]]:
    """Build a Codex user content payload with HA-native image attachments.

    The Responses API expects multimodal user content as an ordered list with
    input_text followed by one input_image item per image. PDFs and other file
    types are intentionally not handled here; add them only when the integration
    explicitly supports those HA attachment MIME types.
    """
    if not images:
        return text

    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    for mime_type, data in images:
        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{encoded}",
                "detail": "auto",
            }
        )
    return content


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
        account_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    except Exception:
        return None
    return account_id if isinstance(account_id, str) and account_id else None


class CodexAuthenticationError(RuntimeError):
    """Raised when Codex rejects the stored access token."""


class CodexRateLimitError(RuntimeError):
    """Raised when Codex rejects a request due to usage or rate limits."""


_RATE_LIMIT_ERROR_CODES = frozenset(
    {
        "rate_limit_exceeded",
        "usage_limit_reached",
        "usage_not_included",
        "quota_exceeded",
    }
)


def _is_rate_limit_response(status_code: int, error: CodexResponseError) -> bool:
    return status_code == 429 or error.code in _RATE_LIMIT_ERROR_CODES


@dataclass(frozen=True)
class CodexResponseError:
    detail: str
    code: str | None = None


def _function_call_item_key(
    item: dict[str, Any], event: dict[str, Any]
) -> str | None:
    for value in (
        item.get("id"),
        event.get("item_id"),
        item.get("call_id"),
        event.get("call_id"),
    ):
        if isinstance(value, str) and value:
            return value
    output_index = event.get("output_index")
    if isinstance(output_index, int):
        return f"output-{output_index}"
    return None


def _function_call_event_key(
    event: dict[str, Any],
    pending_tool_calls: dict[str, tuple[dict[str, Any], str]],
) -> str | None:
    for value in (event.get("item_id"), event.get("call_id")):
        if isinstance(value, str) and value in pending_tool_calls:
            return value
    output_index = event.get("output_index")
    if isinstance(output_index, int):
        key = f"output-{output_index}"
        if key in pending_tool_calls:
            return key
    if len(pending_tool_calls) == 1:
        return next(iter(pending_tool_calls))
    return None


def extract_streamed_output_text(stream_text: str) -> str:
    return extract_streamed_turn_result(stream_text).text


def extract_streamed_turn_result(stream_text: str) -> CodexTurnResult:
    delta_parts: list[str] = []
    done_parts: list[str] = []
    tool_calls: list[CodexToolCall] = []
    pending_tool_calls: dict[str, tuple[dict[str, Any], str]] = {}
    completed_tool_call_ids: set[str] = set()
    anonymous_call_index = 0

    for event in _iter_sse_events(stream_text):
        event_type = event.get("type")
        if stream_error := _stream_event_exception(event):
            raise stream_error
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                delta_parts.append(delta)
            continue
        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                key = _function_call_item_key(item, event)
                if key is None:
                    anonymous_call_index += 1
                    key = f"anonymous-{anonymous_call_index}"
                pending_tool_calls[key] = (item, str(item.get("arguments") or ""))
            continue
        if event_type == "response.function_call_arguments.delta":
            key = _function_call_event_key(event, pending_tool_calls)
            delta = event.get("delta")
            if key is not None and isinstance(delta, str):
                item, arguments = pending_tool_calls[key]
                pending_tool_calls[key] = (item, arguments + delta)
            continue
        if event_type == "response.function_call_arguments.done":
            key = _function_call_event_key(event, pending_tool_calls)
            if key is not None:
                current_tool_call, current_arguments = pending_tool_calls.pop(key)
                arguments = event.get("arguments")
                if isinstance(arguments, str):
                    current_arguments = arguments
                tool_call = _tool_call_from_item(current_tool_call, current_arguments)
                completed_tool_call_ids.add(tool_call.id)
                tool_calls.append(tool_call)
            continue
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                if item.get("type") == "function_call":
                    call_id = str(item.get("call_id") or item.get("id") or "")
                    key = _function_call_item_key(item, event)
                    if key is not None:
                        pending_tool_calls.pop(key, None)
                    if not call_id or call_id not in completed_tool_call_ids:
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
    event_name: str | None = None
    data_lines: list[str] = []

    def flush_event():
        nonlocal event_name
        if not data_lines:
            event_name = None
            return None
        event = event_name
        event_name = None
        data = "\n".join(data_lines)
        data_lines.clear()
        if data == "[DONE]":
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict) and event and "type" not in payload:
            payload["type"] = event
        return payload if isinstance(payload, dict) else None

    for line in stream_text.splitlines():
        if not line.strip():
            payload = flush_event()
            if payload is not None:
                yield payload
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
        elif line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()

    payload = flush_event()
    if payload is not None:
        yield payload


def _response_error(response: Any) -> CodexResponseError:
    text = getattr(response, "text", "") or ""
    try:
        payload = json.loads(text) if text else response.json()
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return CodexResponseError(text[:500] if text else "unknown error")
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload.get("error")
        if isinstance(detail, dict):
            code_value = detail.get("code")
            message = detail.get("message") or detail.get("detail") or code_value
            return CodexResponseError(
                message if isinstance(message, str) else "unknown error",
                code_value if isinstance(code_value, str) else None,
            )
        if isinstance(detail, str):
            return CodexResponseError(detail)
    return CodexResponseError(text[:500] if text else "unknown error")


def _event_error_detail(event: dict[str, Any]) -> str:
    error = event.get("error") or event.get("message") or event.get("detail")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or error.get("code")
        if isinstance(message, str):
            return message
    return "Codex stream returned an error event"


def _stream_event_exception(event: dict[str, Any]) -> RuntimeError | None:
    event_type = event.get("type")
    if event_type == "error":
        return RuntimeError(_event_error_detail(event))
    if event_type == "response.incomplete":
        response = event.get("response")
        details = response.get("incomplete_details") if isinstance(response, dict) else None
        reason = details.get("reason") if isinstance(details, dict) else None
        reason_text = reason if isinstance(reason, str) and reason else "unknown reason"
        return RuntimeError(f"Codex response incomplete: {reason_text}")
    if event_type != "response.failed":
        return None

    response = event.get("response")
    error_value = response.get("error") if isinstance(response, dict) else None
    if isinstance(error_value, dict):
        code_value = error_value.get("code")
        message_value = error_value.get("message") or error_value.get("detail") or code_value
        error = CodexResponseError(
            message_value if isinstance(message_value, str) else "unknown error",
            code_value if isinstance(code_value, str) else None,
        )
    elif isinstance(error_value, str):
        error = CodexResponseError(error_value)
    else:
        error = CodexResponseError("response.failed event received")

    if error.code == "token_invalidated":
        return CodexAuthenticationError(f"Codex authentication failed: {error.detail}")
    if _is_rate_limit_response(200, error):
        return CodexRateLimitError(f"Codex usage limit or rate limit reached: {error.detail}")
    return RuntimeError(f"Codex stream failed: {error.detail}")


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


def _citation_from_annotation(annotation: Any) -> CodexCitation | None:
    if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
        return None
    title = annotation.get("title")
    url = annotation.get("url")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str) or not url.strip():
        return None
    start_index = annotation.get("start_index")
    end_index = annotation.get("end_index")
    return CodexCitation(
        title=title.strip(),
        url=url.strip(),
        start_index=start_index if isinstance(start_index, int) else None,
        end_index=end_index if isinstance(end_index, int) else None,
    )


def _citations_from_event(event: dict[str, Any]) -> list[CodexCitation]:
    annotations: list[Any] = []
    if event.get("type") == "response.output_text.annotation.added":
        annotations.append(event.get("annotation"))

    item = event.get("item")
    if isinstance(item, dict):
        for content in item.get("content") or []:
            if isinstance(content, dict):
                annotations.extend(content.get("annotations") or [])

    return [
        citation
        for annotation in annotations
        if (citation := _citation_from_annotation(annotation)) is not None
    ]


async def _aiter_sse_events(response: Any) -> AsyncIterator[dict[str, Any]]:
    event_name: str | None = None
    data_lines: list[str] = []

    async for line in response.aiter_lines():
        if not line.strip():
            payload = _parse_sse_payload(data_lines, event_name)
            event_name = None
            data_lines.clear()
            if payload is not None:
                yield payload
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
        elif line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()

    payload = _parse_sse_payload(data_lines, event_name)
    if payload is not None:
        yield payload


def _parse_sse_payload(
    data_lines: list[str], event_name: str | None = None
) -> dict[str, Any] | None:
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and event_name and "type" not in payload:
        payload["type"] = event_name
    return payload if isinstance(payload, dict) else None


def _stream_delta_from_event(event: dict[str, Any]) -> CodexStreamDelta | None:
    event_type = event.get("type")
    if stream_error := _stream_event_exception(event):
        raise stream_error
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        return CodexTextDelta(delta) if isinstance(delta, str) and delta else None
    if event_type in {"response.function_call_arguments.done", "response.output_item.done"}:
        item = event.get("item")
        arguments = event.get("arguments")
        if isinstance(item, dict) and item.get("type") == "function_call":
            return CodexToolCallDelta(_tool_call_from_item(item, str(item.get("arguments") or "")))
        if isinstance(arguments, str):
            current_call = event.get("item") or event
            if isinstance(current_call, dict) and (
                current_call.get("call_id") or current_call.get("name")
            ):
                return CodexToolCallDelta(_tool_call_from_item(current_call, arguments))
    return None


async def _stream_response_error(response: Any) -> CodexResponseError:
    try:
        text = await response.aread()
    except Exception:
        text = b""
    text_value = text.decode(errors="replace") if isinstance(text, bytes) else str(text or "")

    class ResponseBody:
        status_code = response.status_code
        text = text_value

        @staticmethod
        def json() -> Any:
            return json.loads(text_value)

    return _response_error(ResponseBody())
