import json

import httpx
import pytest
from homeassistant.components import ai_task, conversation
from homeassistant.core import Context
from test_ai_task_delivery import task_entry as task_entry

from custom_components.codex_assist.codex_client import codex_session_id

COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}
ANSWER = {"type": "response.output_text.delta", "delta": "Done."}
AUTH_FAILURE = {"type": "error", "code": "token_invalidated", "message": "Synthetic rejection"}


@pytest.fixture
async def session_backend(monkeypatch):
    first_events = [ANSWER, COMPLETED]
    sessions = []

    def respond(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={
                "access_token": "test-refreshed", "refresh_token": "test-rotated",
            })
        payload = json.loads(request.content)
        sessions.append((request.headers.get("session_id"), payload.get("prompt_cache_key")))
        events = first_events if len(sessions) == 1 else [ANSWER, COMPLETED]
        return httpx.Response(200, content="".join(
            f"data: {json.dumps(event)}\n\n" for event in events
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for module in ("conversation", "ai_task"):
            monkeypatch.setattr(
                f"custom_components.codex_assist.{module}.get_async_client", lambda hass: client,
            )
        yield first_events, sessions


async def test_every_surface_and_conversation_shares_the_entry_session(
    hass, task_entry, session_backend,
):
    _events, sessions = session_backend
    for _ in range(2):
        await conversation.async_converse(
            hass, "Say done.", None, Context(), agent_id="conversation.codex_assist",
        )
    await ai_task.async_generate_data(
        hass, task_name="Test", instructions="Say done.",
        entity_id="ai_task.codex_assist_ai_task",
    )

    expected = codex_session_id(task_entry.entry_id)
    assert sessions == [(expected, expected)] * 3


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
async def test_auth_retry_keeps_the_entry_session(hass, task_entry, session_backend, surface):
    first_events, sessions = session_backend
    first_events[:] = [AUTH_FAILURE]
    if surface == "conversation":
        await conversation.async_converse(
            hass, "Say done.", None, Context(), agent_id="conversation.codex_assist",
        )
    else:
        await ai_task.async_generate_data(
            hass, task_name="Test", instructions="Say done.",
            entity_id="ai_task.codex_assist_ai_task",
        )

    expected = codex_session_id(task_entry.entry_id)
    assert sessions == [(expected, expected)] * 2
