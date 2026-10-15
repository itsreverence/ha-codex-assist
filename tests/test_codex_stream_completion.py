"""Response success is a protocol event, not a transport EOF."""

import asyncio
import base64
import json

import httpx
import pytest

from custom_components.codex_assist.codex_client import (
    CodexAuthenticationError,
    CodexClient,
    CodexRateLimitError,
    CodexTextDelta,
    CodexToolCallDelta,
)

COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}
TEXT = {"type": "response.output_text.delta", "delta": "Visible text"}
CALL = {
    "type": "function_call", "id": "item-1", "call_id": "call-1",
    "name": "HassTestReading", "arguments": "{}",
}
IMAGE = {
    "type": "response.output_item.done",
    "item": {"type": "image_generation_call", "result": base64.b64encode(b"image").decode()},
}
PREVIEW = {
    "type": "response.image_generation_call.partial_image",
    "partial_image_b64": base64.b64encode(b"preview").decode(),
}


def encode(events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


class Stream(httpx.AsyncByteStream):
    def __init__(self, content, *, gate=False, tail=b"", forbid_tail=False):
        self.content = content
        self.gate = gate
        self.tail = tail
        self.forbid_tail = forbid_tail
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.content
        if self.forbid_tail:
            raise AssertionError("read beyond response.completed")
        if self.gate:
            self.waiting.set()
            await self.release.wait()
        yield self.tail

    async def aclose(self):
        self.closed = True


async def run(client, surface):
    if surface == "image":
        return await client.generate_image(prompt="Draw a test image.")
    return [delta async for delta in client.stream_turn(
        model="gpt-test", instructions="Test", input_items=[],
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface,events", [
    ("text", []), ("text", [TEXT]), ("image", [IMAGE]), ("image", [PREVIEW]),
])
@pytest.mark.parametrize("ending", [b"", b"data: [DONE]\n\n"])
async def test_stream_rejects_eof_without_completed(surface, events, ending):
    stream = Stream(encode(events) + ending)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as http:
        with pytest.raises(RuntimeError, match="before response.completed"):
            await run(CodexClient(http_client=http, access_token="test"), surface)
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("surface,event", [("text", TEXT), ("image", IMAGE)])
@pytest.mark.parametrize("terminal", [
    encode([COMPLETED]),
    b'event: response.completed\r\ndata: {"response":{"status":"completed"}}\r\n\r\n',
])
async def test_completed_stops_reading_without_waiting_for_eof(surface, event, terminal):
    stream = Stream(encode([event]) + terminal, forbid_tail=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as http:
        result = await run(CodexClient(http_client=http, access_token="test"), surface)
    if surface == "image":
        assert result.image_data == b"image"
    else:
        assert result == [CodexTextDelta("Visible text")]
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("surface,event", [("text", TEXT), ("image", PREVIEW)])
async def test_completed_final_event_without_blank_separator(surface, event):
    stream = Stream(encode([event]) + encode([COMPLETED]).rstrip())
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as http:
        result = await run(CodexClient(http_client=http, access_token="test"), surface)
    if surface == "image":
        assert result.image_data == b"preview"
    else:
        assert result == [CodexTextDelta("Visible text")]


@pytest.mark.asyncio
@pytest.mark.parametrize("call_events", [
    [{"type": "response.output_item.done", "item": CALL}],
    [
        {"type": "response.output_item.added", "item": CALL},
        {"type": "response.function_call_arguments.done", "item_id": "item-1", "arguments": "{}"},
        {"type": "response.output_item.done", "item": CALL},
    ],
])
@pytest.mark.parametrize("complete", [False, True])
async def test_text_streams_but_tools_wait_for_response_completion(call_events, complete):
    stream = Stream(encode([TEXT, *call_events]), gate=True,
                    tail=encode([COMPLETED]) if complete else b"")
    deltas = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as http:
        client = CodexClient(http_client=http, access_token="test")

        async def consume():
            async for delta in client.stream_turn(
                model="gpt-test", instructions="Test", input_items=[]
            ):
                deltas.append(delta)

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(stream.waiting.wait(), 2)
            assert [d.text for d in deltas if isinstance(d, CodexTextDelta)] == ["Visible text"]
            assert [d for d in deltas if isinstance(d, CodexToolCallDelta)] == []
        finally:
            stream.release.set()
            if complete:
                await asyncio.wait_for(task, 2)
            else:
                with pytest.raises(RuntimeError, match="before response.completed"):
                    await asyncio.wait_for(task, 2)
    calls = [d.tool_call for d in deltas if isinstance(d, CodexToolCallDelta)]
    assert [(call.id, call.name, call.arguments) for call in calls] == (
        [("call-1", "HassTestReading", {})] if complete else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["text", "image"])
@pytest.mark.parametrize("event,error,message", [
    ({"type": "response.failed", "response": {"error": {"code": "token_invalidated"}}},
     CodexAuthenticationError, "authentication failed"),
    ({"type": "response.failed", "response": {"error": {"code": "rate_limit_exceeded"}}},
     CodexRateLimitError, "rate limit"),
    ({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "limit"}}},
     RuntimeError, "response incomplete: limit"),
    ({"type": "error", "message": "backend down"}, RuntimeError, "backend down"),
    ({"type": "error", "code": "token_invalidated", "error": {
        "code": "quota_exceeded", "message": "Nested quota rejection",
    }}, CodexRateLimitError, "Codex usage limit or rate limit reached: Nested quota rejection"),
    ({"type": "error", "code": "quota_exceeded", "error": {
        "code": "token_invalidated", "detail": "Nested auth rejection",
    }}, CodexAuthenticationError, "Codex authentication failed: Nested auth rejection"),
])
async def test_explicit_stream_failures_keep_their_classification(surface, event, error, message):
    stream = Stream(encode([event]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as http:
        with pytest.raises(error, match=message):
            await run(CodexClient(http_client=http, access_token="test"), surface)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["text", "image"])
@pytest.mark.parametrize("shape", ["flat", "nested", "failed"])
@pytest.mark.parametrize("field", ["message", "detail"])
@pytest.mark.parametrize("code,error,prefix", [
    ("token_invalidated", CodexAuthenticationError, "Codex authentication failed: "),
    ("rate_limit_exceeded", CodexRateLimitError, "Codex usage limit or rate limit reached: "),
    ("usage_limit_reached", CodexRateLimitError, "Codex usage limit or rate limit reached: "),
    ("usage_not_included", CodexRateLimitError, "Codex usage limit or rate limit reached: "),
    ("quota_exceeded", CodexRateLimitError, "Codex usage limit or rate limit reached: "),
])
async def test_structured_stream_errors(surface, shape, field, code, error, prefix):
    detail = {"code": code, field: "Synthetic rejection"}
    event = ({"type": "error", **detail} if shape == "flat" else
             {"type": "error", "error": detail} if shape == "nested" else
             {"type": "response.failed", "response": {"error": detail}})
    stream = Stream(encode([event]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as http:
        with pytest.raises(error) as caught:
            await run(CodexClient(http_client=http, access_token="test"), surface)
    assert str(caught.value) == prefix + "Synthetic rejection"
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["text", "image"])
@pytest.mark.parametrize("shape", ["flat", "nested", "failed"])
@pytest.mark.parametrize("code", [None, 401, [], {}, "unknown", "prefix_token_invalidated",
                                  "quota_exceeded_suffix"])
async def test_unrecognized_stream_codes_preserve_generic_diagnostics(surface, shape, code):
    detail = {"code": code, "message": "token_invalidated quota_exceeded"}
    event = ({"type": "error", **detail} if shape == "flat" else
             {"type": "error", "error": detail} if shape == "nested" else
             {"type": "response.failed", "response": {"error": detail}})
    stream = Stream(encode([event]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as http:
        with pytest.raises(RuntimeError) as caught:
            await run(CodexClient(http_client=http, access_token="test"), surface)
    assert type(caught.value) is RuntimeError
    prefix = "Codex stream failed: " if shape == "failed" else ""
    assert str(caught.value) == prefix + "token_invalidated quota_exceeded"
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["text", "image"])
@pytest.mark.parametrize("event,message", [
    ({"type": "error"}, "Codex stream returned an error event"),
    ({"type": "error", "code": "unknown"}, "Codex stream returned an error event"),
    ({"type": "error", "error": "backend down"}, "backend down"),
    ({"type": "error", "error": {"code": "unknown"}}, "unknown"),
    ({"type": "error", "error": {"code": []}}, "Codex stream returned an error event"),
    ({"type": "error", "code": "token_invalidated", "error": {"code": "unknown"}},
     "unknown"),
    ({"type": "error", "code": "token_invalidated", "error": {}, "message": "backend down"},
     "backend down"),
    ({"type": "error", "code": "token_invalidated", "error": {
        "code": [], "detail": "Malformed nested code",
    }}, "Malformed nested code"),
    ({"type": "error", "error": ["token_invalidated"]},
     "Codex stream returned an error event"),
    ({"type": "response.failed"}, "Codex stream failed: response.failed event received"),
    ({"type": "response.failed", "response": {"error": {"code": {}}}},
     "Codex stream failed: unknown error"),
    ({"type": "response.incomplete", "response": {"error": {"code": "token_invalidated"}}},
     "Codex response incomplete: unknown reason"),
])
async def test_malformed_stream_errors_keep_existing_diagnostics(surface, event, message):
    stream = Stream(encode([event]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as http:
        with pytest.raises(RuntimeError) as caught:
            await run(CodexClient(http_client=http, access_token="test"), surface)
    assert type(caught.value) is RuntimeError
    assert str(caught.value) == message
