import json

import pytest

from custom_components.codex_assist.codex_client import (
    CodexClient,
    CodexTextDelta,
    CodexToolCallDelta,
)


class FakeStreamResponse:
    def __init__(self, status_code, lines, *, body=b""):
        self.status_code = status_code
        self._lines = lines
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self._body


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, **kwargs):
        raise AssertionError("stream tests should not call post")

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def _event(payload):
    return ["data: " + json.dumps(payload), ""]


@pytest.mark.asyncio
async def test_stream_turn_yields_text_deltas_and_posts_advanced_options():
    response = FakeStreamResponse(
        200,
        _event({"type": "response.output_text.delta", "delta": "Hel"})
        + _event({"type": "response.output_text.delta", "delta": "lo"}),
    )
    http = FakeHttpClient(response)
    client = CodexClient(http_client=http, access_token="token-1")

    deltas = [
        delta
        async for delta in client.stream_turn(
            model="gpt-5.4",
            instructions="Be concise.",
            input_items=[{"role": "user", "content": "ping"}],
            reasoning_effort="medium",
            reasoning_summary="auto",
            text_verbosity="low",
        )
    ]

    assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == [
        "Hel",
        "lo",
    ]
    payload = http.calls[0][2]["json"]
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert payload["text"] == {"verbosity": "low"}


@pytest.mark.asyncio
async def test_stream_turn_yields_function_call_after_arguments_complete():
    response = FakeStreamResponse(
        200,
        _event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "HassTurnOn",
                },
            }
        )
        + _event(
            {
                "type": "response.function_call_arguments.delta",
                "delta": '{"name":"Kitchen"',
            }
        )
        + _event(
            {
                "type": "response.function_call_arguments.done",
                "arguments": '{"name":"Kitchen","domain":"light"}',
            }
        ),
    )
    client = CodexClient(http_client=FakeHttpClient(response), access_token="token-1")

    deltas = [
        delta
        async for delta in client.stream_turn(
            model="gpt-5.4",
            instructions="Use tools.",
            input_items=[{"role": "user", "content": "turn on kitchen"}],
            tools=[{"type": "function", "name": "HassTurnOn", "parameters": {}}],
        )
    ]

    tool_delta = next(delta for delta in deltas if isinstance(delta, CodexToolCallDelta))
    assert tool_delta.tool_call.id == "call-1"
    assert tool_delta.tool_call.name == "HassTurnOn"
    assert tool_delta.tool_call.arguments == {"name": "Kitchen", "domain": "light"}


@pytest.mark.asyncio
async def test_stream_turn_omits_reasoning_summary_when_off():
    response = FakeStreamResponse(200, [])
    http = FakeHttpClient(response)
    client = CodexClient(http_client=http, access_token="token-1")

    deltas = [
        delta
        async for delta in client.stream_turn(
            model="gpt-5.4",
            instructions="x",
            input_items=[],
            reasoning_effort="low",
            reasoning_summary="off",
        )
    ]

    assert deltas == []
    assert http.calls[0][2]["json"]["reasoning"] == {"effort": "low"}
