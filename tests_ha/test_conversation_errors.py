"""Conversation error delivery through real HA and synthetic backend HTTP."""

import asyncio
import base64
import json
import logging

import httpx
import pytest
import voluptuous as vol
from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.helpers import llm
from test_ai_task_delivery import task_entry as task_entry

GENERIC = "Sorry, I couldn't complete that."
AUTH = "Codex Assist needs attention in Home Assistant."
QUOTA = "Codex Assist is temporarily unavailable. Please try again later."
MODEL_PATH = "/backend-api/codex/responses"
TOKEN_PATH = "/oauth/token"
MARKERS = ("synthetic-secret-body", "synthetic-secret-chain", "https://private.example.test")
COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}


def sse(*events):
    return httpx.Response(200, content="".join(
        f"data: {json.dumps(event)}\n\n" for event in events
    ))


def failure(code="server_error"):
    return sse({"type": "response.failed", "response": {"error": {
        "code": code, "message": " ".join(MARKERS),
    }}})


def chained(error_type):
    try:
        raise RuntimeError(MARKERS[1])
    except RuntimeError as cause:
        try:
            raise error_type(f"{MARKERS[0]} {MARKERS[2]}") from cause
        except BaseException as err:
            return err


@pytest.fixture
async def error_backend(monkeypatch):
    planned = []
    requests = []

    def respond(request):
        payload = json.loads(request.content) if request.url.path == MODEL_PATH else None
        requests.append((request.url.path, payload))
        assert planned, "Unexpected backend request"
        path, reply = planned.pop(0)
        assert request.url.path == path
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            "custom_components.codex_assist.conversation.get_async_client", lambda hass: client,
        )
        yield planned, requests
    assert not planned


async def converse(hass, conversation_id=None):
    return await conversation.async_converse(
        hass, "Give a test answer.", conversation_id, Context(),
        agent_id="conversation.codex_assist",
    )


async def assert_safe_delivery(hass, result, error_backend, caplog, expected):
    assert result.response.speech["plain"]["speech"] == expected
    assert not result.response.card
    planned, requests = error_backend
    planned.append((MODEL_PATH, sse(
        {"type": "response.output_text.delta", "delta": "A follow-up answer."}, COMPLETED,
    )))
    followup = await converse(hass, result.conversation_id)
    assert followup.response.speech["plain"]["speech"] == "A follow-up answer."
    history = json.dumps(requests[-1][1]["input"])
    records = [record for record in caplog.records
               if record.name == "custom_components.codex_assist.conversation"]
    assert records, "Expected a conversation diagnostic"
    assert all(record.exc_info is None and record.stack_info is None for record in records)
    for record in records:
        for field in ("stage=", "exception=", "retry=", "reauth="):
            assert field in record.getMessage()
    logs = "\n".join(record.getMessage() for record in records)
    for marker in MARKERS:
        assert marker not in history
        assert marker not in logs
        assert marker not in str(result.as_dict())


@pytest.mark.parametrize("kind", [
    "http", "sse", "transport", "blank-transport", "quota-http", "quota-sse",
])
async def test_backend_errors_have_fixed_speech_and_safe_history_and_logs(
    hass, task_entry, error_backend, caplog, kind,
):
    caplog.set_level(logging.DEBUG, logger="custom_components.codex_assist.conversation")
    planned, requests = error_backend
    replies = {
        "http": httpx.Response(503, text=" ".join(MARKERS)),
        "sse": failure(),
        "transport": chained(httpx.ReadError),
        "blank-transport": httpx.ReadTimeout(""),
        "quota-http": httpx.Response(429, text=" ".join(MARKERS)),
        "quota-sse": failure("quota_exceeded"),
    }
    planned.append((MODEL_PATH, replies[kind]))
    result = await converse(hass)
    assert [path for path, _ in requests] == [MODEL_PATH]
    assert not hass.config_entries.flow.async_progress()
    await assert_safe_delivery(
        hass, result, error_backend, caplog, QUOTA if kind.startswith("quota") else GENERIC,
    )


@pytest.mark.parametrize("stage", [
    "resolve", "refresh", "retry", "retry-success", "temporary", "temporary-resolve", "visible",
])
async def test_auth_error_decisions_and_fixed_delivery(
    hass, task_entry, error_backend, caplog, stage,
):
    planned, requests = error_backend
    rejected = httpx.Response(401, text=" ".join(MARKERS))
    if stage == "resolve":
        hass.config_entries.async_update_entry(task_entry, data={
            **task_entry.data, "access_token": "",
        })
        expected_paths = []
    elif stage == "temporary-resolve":
        payload = base64.urlsafe_b64encode(b'{"exp": 0}').decode().rstrip("=")
        hass.config_entries.async_update_entry(task_entry, data={
            **task_entry.data, "access_token": f"header.{payload}.signature",
        })
        planned.append((TOKEN_PATH, chained(httpx.ReadTimeout)))
        expected_paths = [TOKEN_PATH]
    elif stage == "visible":
        planned.append((MODEL_PATH, sse(
            {"type": "response.output_text.delta", "delta": "A partial answer."},
            {"type": "error", "code": "token_invalidated", "message": " ".join(MARKERS)},
        )))
        expected_paths = [MODEL_PATH]
    else:
        planned.append((MODEL_PATH, rejected))
        if stage in {"retry", "retry-success"}:
            planned.extend([
                (TOKEN_PATH, httpx.Response(200, json={
                    "access_token": "test-refreshed", "refresh_token": "test-rotated",
                })),
                (MODEL_PATH, failure("token_invalidated") if stage == "retry" else sse(
                    {"type": "response.output_text.delta", "delta": "A recovered answer."},
                    COMPLETED,
                )),
            ])
            expected_paths = [MODEL_PATH, TOKEN_PATH, MODEL_PATH]
        else:
            planned.append((TOKEN_PATH, chained(httpx.ReadTimeout)
                            if stage == "temporary" else rejected))
            expected_paths = [MODEL_PATH, TOKEN_PATH]
    result = await converse(hass)
    await hass.async_block_till_done()
    assert [path for path, _ in requests] == expected_paths
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == (1 if stage in {"resolve", "refresh", "retry"} else 0)
    if flows:
        assert flows[0]["context"]["source"] == "reauth"
    if stage in {"resolve", "temporary-resolve"}:
        hass.config_entries.async_update_entry(task_entry, data={
            **task_entry.data, "access_token": "test-restored",
        })
    expected = (
        "A recovered answer." if stage == "retry-success"
        else GENERIC if stage.startswith("temporary") else AUTH
    )
    await assert_safe_delivery(hass, result, error_backend, caplog, expected)


@pytest.fixture
def native_tool(monkeypatch):
    executed = []
    errors = []

    class Reading(llm.Tool):
        name = "HassTestReading"
        description = "Return a synthetic reading."
        parameters = vol.Schema({})

        async def async_call(self, hass, tool_input, llm_context):
            executed.append(tool_input.id)
            if errors:
                raise errors.pop(0)
            data = {"reading": 7}
            return llm.ToolResult(data=data) if hasattr(llm, "ToolResult") else data

    class ReadingAPI(llm.API):
        async def async_get_api_instance(self, llm_context):
            return llm.APIInstance(
                api=self, api_prompt="A test reading is available.",
                llm_context=llm_context, tools=[Reading()],
            )

    async def get_api(hass, api_id, llm_context):
        api = ReadingAPI(hass=hass, id="error_test", name="Error test")
        return await api.async_get_api_instance(llm_context)

    monkeypatch.setattr(llm, "async_get_api", get_api)
    return executed, errors


@pytest.mark.parametrize("error_type", [ValueError, TypeError, None])
async def test_native_tool_failure_or_failed_synthesis_never_replays_action(
    hass, task_entry, error_backend, native_tool, caplog, error_type,
):
    executed, errors = native_tool
    planned, requests = error_backend
    planned.append((MODEL_PATH, sse({"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "reading-1", "name": "HassTestReading",
        "arguments": "{}", "status": "completed",
    }}, COMPLETED)))
    if error_type:
        errors.append(chained(error_type))
    else:
        planned.append((MODEL_PATH, failure()))
    result = await converse(hass)
    assert executed == ["reading-1"]
    assert [path for path, _ in requests] == [MODEL_PATH] * (1 if error_type else 2)
    await assert_safe_delivery(hass, result, error_backend, caplog, GENERIC)
    assert executed == ["reading-1"]


async def test_conversation_cancellation_propagates(hass, task_entry, error_backend):
    planned, requests = error_backend
    planned.append((MODEL_PATH, asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await converse(hass)
    assert [path for path, _ in requests] == [MODEL_PATH]
    assert not hass.config_entries.flow.async_progress()


@pytest.mark.parametrize("web_search", [False, True])
async def test_conversation_sends_error_guidance_with_custom_prompt(
    hass, task_entry, error_backend, web_search,
):
    hass.config_entries.async_update_entry(task_entry, options={
        **task_entry.options, "prompt": "Use a friendly tone.", "web_search": web_search,
    })
    planned, requests = error_backend
    planned.append((MODEL_PATH, sse(
        {"type": "response.output_text.delta", "delta": "A test answer."}, COMPLETED,
    )))
    result = await converse(hass)
    assert result.response.speech["plain"]["speech"] == "A test answer."
    instructions = requests[0][1]["instructions"]
    assert "Use a friendly tone." in instructions
    assert "short" in instructions and "error" in instructions
    for detail in ("diagnostics", "error codes", "request IDs", "HTTP details", "stack traces",
                   "reporting instructions", "troubleshooting"):
        assert detail in instructions
    assert ("do not include raw URLs" in instructions) == web_search
    assert [path for path, _ in requests] == [MODEL_PATH]
