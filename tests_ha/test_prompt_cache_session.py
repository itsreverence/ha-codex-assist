import base64
import json
from types import MappingProxyType

import httpx
import pytest
from codex_entries import subentry_of
from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import Context
from test_ai_task_delivery import PNG_BYTES
from test_ai_task_delivery import task_entry as task_entry

from custom_components.codex_assist.codex_client import codex_session_id

COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}
ANSWER = {"type": "response.output_text.delta", "delta": "Done."}
IMAGE = {"type": "response.output_item.done", "item": {
    "type": "image_generation_call", "result": base64.b64encode(PNG_BYTES).decode(),
}}
AUTH_FAILURE = {"type": "error", "code": "token_invalidated", "message": "Synthetic rejection"}


@pytest.fixture
async def session_backend(monkeypatch):
    fail_first = []
    sessions = []

    def respond(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={
                "access_token": "test-refreshed", "refresh_token": "test-rotated",
            })
        payload = json.loads(request.content)
        sessions.append((request.headers.get("session_id"), payload.get("prompt_cache_key")))
        if fail_first and len(sessions) == 1:
            events = [AUTH_FAILURE]
        elif any(tool.get("type") == "image_generation" for tool in payload.get("tools", [])):
            events = [IMAGE, COMPLETED]
        else:
            events = [ANSWER, COMPLETED]
        return httpx.Response(200, content="".join(
            f"data: {json.dumps(event)}\n\n" for event in events
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for module in ("conversation", "ai_task"):
            monkeypatch.setattr(
                f"custom_components.codex_assist.{module}.get_async_client", lambda hass: client,
            )
        yield fail_first, sessions


async def _run(hass, surface):
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Say done.", None, Context(), agent_id="conversation.codex_assist",
        )
        assert result.response.speech["plain"]["speech"] == "Done."
    elif surface == "data":
        result = await ai_task.async_generate_data(
            hass, task_name="Test", instructions="Say done.",
            entity_id="ai_task.codex_assist_ai_task",
        )
        assert result.data == "Done."
    else:
        result = await ai_task.async_generate_image(
            hass, task_name="Test", instructions="Draw a lamp.",
            entity_id="ai_task.codex_assist_ai_task",
        )
        assert result["mime_type"] == "image/png"


def _session(entry, subentry_type):
    return codex_session_id(subentry_of(entry, subentry_type).subentry_id)


async def test_each_agent_keeps_its_own_session(hass, task_entry, session_backend):
    _fail_first, sessions = session_backend
    for surface in ("conversation", "conversation", "data", "image"):
        await _run(hass, surface)

    conversation_session = _session(task_entry, "conversation")
    task_session = _session(task_entry, "ai_task_data")
    assert conversation_session != task_session
    assert sessions == (
        [(conversation_session, conversation_session)] * 2 + [(task_session, task_session)] * 2
    )


async def test_added_agent_gets_a_separate_session_and_the_first_keeps_its_own(
    hass, task_entry, session_backend,
):
    _fail_first, sessions = session_backend
    first_session = _session(task_entry, "conversation")
    hass.config_entries.async_add_subentry(task_entry, ConfigSubentry(
        data=MappingProxyType({"model": "gpt-5.4", "prompt": "Answer as a second agent."}),
        subentry_type="conversation", title="Second agent", unique_id=None,
    ))
    await hass.async_block_till_done()
    second = next(s for s in task_entry.subentries.values() if s.title == "Second agent")

    await _run(hass, "conversation")
    result = await conversation.async_converse(
        hass, "Say done.", None, Context(), agent_id="conversation.second_agent",
    )
    assert result.response.speech["plain"]["speech"] == "Done."

    second_session = codex_session_id(second.subentry_id)
    assert second_session != first_session
    assert sessions == [(first_session, first_session), (second_session, second_session)]


@pytest.mark.parametrize("surface", ["conversation", "data", "image"])
async def test_auth_retry_keeps_the_agent_session(hass, task_entry, session_backend, surface):
    fail_first, sessions = session_backend
    fail_first.append(True)
    await _run(hass, surface)

    expected = _session(task_entry, "conversation" if surface == "conversation" else "ai_task_data")
    assert sessions == [(expected, expected)] * 2
