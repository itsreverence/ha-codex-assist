import base64
import json
import logging

import httpx
import pytest

from custom_components.codex_assist.codex_client import CodexClient, codex_session_id


def _sse(*events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


COMPLETED_WITH_USAGE = {
    "type": "response.completed",
    "response": {
        "status": "completed",
        "usage": {
            "input_tokens": 6350,
            "input_tokens_details": {"cached_tokens": 6144},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 6355,
        },
    },
}


def _recording_client(body, *, session_id):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CodexClient(http_client=http, access_token="token-1", session_id=session_id)
    return http, client, requests


def test_codex_session_id_is_stable_per_entry_and_hides_the_entry_id():
    first = codex_session_id("01JENTRYAAAA")

    assert first == codex_session_id("01JENTRYAAAA")
    assert first != codex_session_id("01JENTRYBBBB")
    assert "01JENTRYAAAA" not in first


@pytest.mark.asyncio
async def test_stream_turn_sends_session_header_and_prompt_cache_key():
    http, client, requests = _recording_client(_sse(COMPLETED_WITH_USAGE), session_id="session-1")
    async with http:
        [
            delta
            async for delta in client.stream_turn(
                model="gpt-5.4",
                instructions="You are a Home Assistant voice agent.",
                input_items=[{"role": "user", "content": "turn on kitchen"}],
            )
        ]

    [request] = requests
    assert request.headers["session_id"] == "session-1"
    assert json.loads(request.content)["prompt_cache_key"] == "session-1"


@pytest.mark.asyncio
async def test_generate_image_sends_session_header_and_prompt_cache_key():
    image = {
        "type": "response.output_item.done",
        "item": {"type": "image_generation_call", "result": base64.b64encode(b"png").decode()},
    }
    http, client, requests = _recording_client(
        _sse(image, COMPLETED_WITH_USAGE), session_id="session-1"
    )
    async with http:
        await client.generate_image(prompt="Draw a lamp")

    [request] = requests
    assert request.headers["session_id"] == "session-1"
    assert json.loads(request.content)["prompt_cache_key"] == "session-1"


@pytest.mark.asyncio
async def test_requests_without_session_omit_session_fields():
    http, client, requests = _recording_client(_sse(COMPLETED_WITH_USAGE), session_id=None)
    async with http:
        [
            delta
            async for delta in client.stream_turn(
                model="gpt-5.4", instructions="Answer.", input_items=[]
            )
        ]

    [request] = requests
    assert "session_id" not in request.headers
    assert "prompt_cache_key" not in json.loads(request.content)


@pytest.mark.asyncio
async def test_completed_response_usage_is_logged_at_debug(caplog):
    http, client, _requests = _recording_client(_sse(COMPLETED_WITH_USAGE), session_id="s")
    with caplog.at_level(logging.DEBUG, logger="custom_components.codex_assist.codex_client"):
        async with http:
            [
                delta
                async for delta in client.stream_turn(
                    model="gpt-5.4", instructions="Answer.", input_items=[]
                )
            ]

    assert "Codex usage: input=6350 cached=6144 output=5 reasoning=0" in caplog.messages


@pytest.mark.asyncio
async def test_usage_log_omits_values_that_are_not_token_counts(caplog):
    completed = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "usage": {
                "input_tokens": "synthetic text",
                "input_tokens_details": {"cached_tokens": {"nested": "synthetic"}},
                "output_tokens": True,
                "output_tokens_details": {"reasoning_tokens": -1},
            },
        },
    }
    http, client, _requests = _recording_client(_sse(completed), session_id="s")
    with caplog.at_level(logging.DEBUG, logger="custom_components.codex_assist.codex_client"):
        async with http:
            [
                delta
                async for delta in client.stream_turn(
                    model="gpt-5.4", instructions="Answer.", input_items=[]
                )
            ]

    assert "Codex usage: input=None cached=None output=None reasoning=None" in caplog.messages
