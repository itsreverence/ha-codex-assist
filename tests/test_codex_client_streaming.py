import json

import pytest

from custom_components.codex_assist.codex_client import CodexClient, CodexMessage


class FakeResponse:
    def __init__(self, status_code, *, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _sse_event(event_type, data):
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


@pytest.mark.asyncio
async def test_generate_text_requests_required_codex_streaming_shape():
    response = FakeResponse(
        200,
        text=_sse_event(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "Pong"},
        )
        + _sse_event("response.completed", {"type": "response.completed"}),
    )
    http = FakeHttpClient(response)
    client = CodexClient(
        http_client=http,
        access_token="token-1",
    )

    result = await client.generate_text(
        model="gpt-5.4",
        instructions="You are concise.",
        messages=[CodexMessage(role="user", content="ping")],
    )

    assert result == "Pong"
    _, kwargs = http.calls[0]
    assert kwargs["json"]["stream"] is True
    assert kwargs["headers"]["Accept"] == "text/event-stream"
    assert kwargs["headers"]["originator"] == "codex_cli_rs"
    assert kwargs["headers"]["User-Agent"].startswith("codex_cli_rs/")


@pytest.mark.asyncio
async def test_generate_text_does_not_duplicate_stream_delta_and_done_item_text():
    text = "Codex Assist is connected."
    response = FakeResponse(
        200,
        text=_sse_event(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": text},
        )
        + _sse_event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                },
            },
        )
        + _sse_event("response.completed", {"type": "response.completed"}),
    )
    client = CodexClient(http_client=FakeHttpClient(response), access_token="token-1")

    result = await client.generate_text(
        model="gpt-5.4",
        instructions="You are concise.",
        messages=[CodexMessage(role="user", content="ping")],
    )

    assert result == text


@pytest.mark.asyncio
async def test_generate_text_handles_crlf_sse_and_done_sentinel():
    response = FakeResponse(
        200,
        text=(
            'event: response.output_text.delta\r\n'
            'data: {"type":"response.output_text.delta","delta":"pong"}\r\n'
            '\r\n'
            'data: [DONE]\r\n'
            '\r\n'
        ),
    )
    client = CodexClient(http_client=FakeHttpClient(response), access_token="token-1")

    result = await client.generate_text(
        model="gpt-5.4",
        instructions="You are concise.",
        messages=[CodexMessage(role="user", content="ping")],
    )

    assert result == "pong"


@pytest.mark.asyncio
async def test_generate_turn_extracts_streamed_function_call_arguments():
    response = FakeResponse(
        200,
        text=_sse_event(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "call_id": "call-1", "name": "HassTurnOff"},
            },
        )
        + _sse_event(
            "response.function_call_arguments.delta",
            {"type": "response.function_call_arguments.delta", "delta": '{"name":"Hallway"'},
        )
        + _sse_event(
            "response.function_call_arguments.done",
            {
                "type": "response.function_call_arguments.done",
                "arguments": '{"name":"Hallway","domain":"light"}',
            },
        ),
    )
    client = CodexClient(http_client=FakeHttpClient(response), access_token="token-1")

    result = await client.generate_turn(
        model="gpt-5.4",
        instructions="Use tools.",
        input_items=[{"role": "user", "content": "turn off hallway"}],
        tools=[{"type": "function", "name": "HassTurnOff", "parameters": {}}],
    )

    assert result.text == ""
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "HassTurnOff"
    assert result.tool_calls[0].arguments == {"name": "Hallway", "domain": "light"}


@pytest.mark.asyncio
async def test_generate_text_surfaces_codex_error_body_for_debugging():
    client = CodexClient(
        http_client=FakeHttpClient(
            FakeResponse(400, text='{"detail":"Stream must be set to true"}')
        ),
        access_token="token-1",
    )

    with pytest.raises(RuntimeError, match="Stream must be set to true"):
        await client.generate_text(
            model="gpt-5.4",
            instructions="x",
            messages=[CodexMessage(role="user", content="hello")],
        )
