import json

import pytest

from custom_components.codex_assist.codex_client import (
    CODEX_STREAM_TIMEOUT,
    CodexCitationDelta,
    CodexClient,
    CodexRateLimitError,
    CodexResponseItemDelta,
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
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["text"] == {"verbosity": "low"}
    assert http.calls[0][2]["timeout"] == CODEX_STREAM_TIMEOUT


@pytest.mark.asyncio
async def test_stream_turn_posts_structured_output_format_with_verbosity():
    response = FakeStreamResponse(200, [])
    http = FakeHttpClient(response)
    client = CodexClient(http_client=http, access_token="token-1")
    text_format = {
        "type": "json_schema",
        "name": "porch_state",
        "schema": {
            "type": "object",
            "properties": {"state": {"type": "string"}},
            "required": ["state"],
        },
    }

    deltas = [
        delta
        async for delta in client.stream_turn(
            model="gpt-5.4",
            instructions="Return structured data.",
            input_items=[{"role": "user", "content": "porch state"}],
            text_verbosity="low",
            text_format=text_format,
        )
    ]

    assert deltas == []
    assert http.calls[0][2]["json"]["text"] == {
        "verbosity": "low",
        "format": text_format,
    }


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
async def test_stream_turn_correlates_interleaved_function_call_arguments_by_item_id():
    response = FakeStreamResponse(
        200,
        _event(
            {
                "type": "response.output_item.added",
                "item": {
                    "id": "item-1",
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "HassTurnOn",
                },
            }
        )
        + _event(
            {
                "type": "response.output_item.added",
                "item": {
                    "id": "item-2",
                    "type": "function_call",
                    "call_id": "call-2",
                    "name": "HassSetPosition",
                },
            }
        )
        + _event(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "item-1",
                "delta": '{"name":"Kitchen"',
            }
        )
        + _event(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "item-2",
                "delta": '{"name":"Shade"',
            }
        )
        + _event(
            {
                "type": "response.function_call_arguments.done",
                "item_id": "item-2",
                "arguments": '{"name":"Shade","position":50}',
            }
        )
        + _event(
            {
                "type": "response.function_call_arguments.done",
                "item_id": "item-1",
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
            input_items=[{"role": "user", "content": "turn on kitchen and move shade"}],
            tools=[
                {"type": "function", "name": "HassTurnOn", "parameters": {}},
                {"type": "function", "name": "HassSetPosition", "parameters": {}},
            ],
        )
    ]

    tool_calls = [
        delta.tool_call for delta in deltas if isinstance(delta, CodexToolCallDelta)
    ]
    assert [(call.id, call.name, call.arguments) for call in tool_calls] == [
        ("call-2", "HassSetPosition", {"name": "Shade", "position": 50}),
        ("call-1", "HassTurnOn", {"name": "Kitchen", "domain": "light"}),
    ]


@pytest.mark.asyncio
async def test_stream_turn_yields_structured_web_citations_and_requests_sources():
    citation = {
        "type": "url_citation",
        "title": "IANA Reserved Domains",
        "url": "https://www.iana.org/help/example-domains",
        "start_index": 10,
        "end_index": 22,
    }
    response = FakeStreamResponse(
        200,
        _event(
            {
                "type": "response.output_text.annotation.added",
                "annotation": citation,
            }
        )
        + _event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Example Domains are maintained by IANA.",
                            "annotations": [citation],
                        }
                    ],
                },
            }
        ),
    )
    http = FakeHttpClient(response)
    client = CodexClient(http_client=http, access_token="token-1")

    deltas = [
        delta
        async for delta in client.stream_turn(
            model="gpt-5.4",
            instructions="Use search.",
            input_items=[{"role": "user", "content": "Who maintains Example Domains?"}],
            tools=[{"type": "web_search"}],
            reasoning_effort="low",
        )
    ]

    citations = [delta.citation for delta in deltas if isinstance(delta, CodexCitationDelta)]
    assert [(citation.title, citation.url) for citation in citations] == [
        ("IANA Reserved Domains", "https://www.iana.org/help/example-domains"),
        ("IANA Reserved Domains", "https://www.iana.org/help/example-domains"),
    ]
    assert http.calls[0][2]["json"]["include"] == [
        "reasoning.encrypted_content",
        "web_search_call.action.sources",
    ]


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


@pytest.mark.asyncio
async def test_stream_turn_omits_advanced_options_for_non_reasoning_models():
    response = FakeStreamResponse(200, [])
    http = FakeHttpClient(response)
    client = CodexClient(http_client=http, access_token="token-1")

    deltas = [
        delta
        async for delta in client.stream_turn(
            model="custom-fast-model",
            instructions="x",
            input_items=[],
            reasoning_effort="low",
            reasoning_summary="auto",
            text_verbosity="medium",
        )
    ]

    assert deltas == []
    payload = http.calls[0][2]["json"]
    assert "reasoning" not in payload
    assert "include" not in payload
    assert "text" not in payload


@pytest.mark.asyncio
async def test_stream_turn_raises_rate_limit_error_for_429():
    response = FakeStreamResponse(
        429,
        [],
        body=b'{"error":"quota exceeded"}',
    )
    client = CodexClient(http_client=FakeHttpClient(response), access_token="token-1")

    with pytest.raises(CodexRateLimitError, match="quota exceeded"):
        async for _delta in client.stream_turn(
            model="gpt-5.4",
            instructions="x",
            input_items=[{"role": "user", "content": "hello"}],
        ):
            pass


@pytest.mark.asyncio
async def test_stream_turn_raises_rate_limit_error_for_failed_stream_event():
    response = FakeStreamResponse(
        200,
        _event(
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "synthetic quota failure",
                    }
                },
            }
        ),
    )
    client = CodexClient(http_client=FakeHttpClient(response), access_token="token-1")

    with pytest.raises(CodexRateLimitError, match="synthetic quota failure"):
        async for _delta in client.stream_turn(
            model="gpt-5.4",
            instructions="x",
            input_items=[{"role": "user", "content": "hello"}],
        ):
            pass


@pytest.mark.asyncio
async def test_stream_turn_raises_for_incomplete_stream_event():
    response = FakeStreamResponse(
        200,
        _event(
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}},
            }
        ),
    )
    client = CodexClient(http_client=FakeHttpClient(response), access_token="token-1")

    with pytest.raises(RuntimeError, match="incomplete.*max_output_tokens"):
        async for _delta in client.stream_turn(
            model="gpt-5.4",
            instructions="x",
            input_items=[{"role": "user", "content": "hello"}],
        ):
            pass


@pytest.mark.asyncio
async def test_stream_turn_preserves_output_item_done_payloads_exactly():
    output_items = [
        {
            "id": "rs_1",
            "type": "reasoning",
            "encrypted_content": "encrypted-state",
            "summary": [],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "HassTurnOn",
            "arguments": '{"name":"Kitchen"}',
            "status": "completed",
        },
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Done.", "annotations": []}],
            "status": "completed",
        },
    ]
    response = FakeStreamResponse(
        200,
        sum(
            (
                _event({"type": "response.output_item.done", "item": item})
                for item in output_items
            ),
            [],
        ),
    )
    client = CodexClient(http_client=FakeHttpClient(response), access_token="token-1")

    deltas = [
        delta
        async for delta in client.stream_turn(
            model="gpt-5.4",
            instructions="Use tools.",
            input_items=[{"role": "user", "content": "turn on kitchen"}],
        )
    ]

    assert [
        delta.item for delta in deltas if isinstance(delta, CodexResponseItemDelta)
    ] == output_items
