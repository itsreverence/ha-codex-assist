import base64
import json

import httpx
import pytest
from homeassistant.components import ai_task, conversation
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


async def test_every_surface_and_conversation_shares_the_entry_session(
    hass, task_entry, session_backend,
):
    _fail_first, sessions = session_backend
    for surface in ("conversation", "conversation", "data", "image"):
        await _run(hass, surface)

    expected = codex_session_id(task_entry.entry_id)
    assert sessions == [(expected, expected)] * 4


@pytest.mark.parametrize("surface", ["conversation", "data", "image"])
async def test_auth_retry_keeps_the_entry_session(hass, task_entry, session_backend, surface):
    fail_first, sessions = session_backend
    fail_first.append(True)
    await _run(hass, surface)

    expected = codex_session_id(task_entry.entry_id)
    assert sessions == [(expected, expected)] * 2
