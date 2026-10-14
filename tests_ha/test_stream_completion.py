import base64
import json

import httpx
import pytest
import voluptuous as vol
from homeassistant.components import ai_task, conversation
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from test_ai_task_delivery import PNG_BYTES
from test_ai_task_delivery import task_entry as task_entry

COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}
TEXT = {"type": "response.output_text.delta", "delta": "An unfinished answer."}
AUTH_FAILURE = {
    "type": "response.failed", "response": {"error": {
        "code": "token_invalidated", "message": "Synthetic auth rejection",
    }},
}


@pytest.fixture
async def stream_backend(monkeypatch):
    events = []
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={
                "access_token": "test-refreshed", "refresh_token": "test-rotated",
            })
        assert request.url.path == "/backend-api/codex/responses"
        payload = events if len(requests) == 1 else [
            {"type": "response.output_text.delta", "delta": "A complete answer."}, COMPLETED,
        ]
        return httpx.Response(200, content="".join(
            f"data: {json.dumps(event)}\n\n" for event in payload
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for module in ("conversation", "ai_task"):
            monkeypatch.setattr(
                f"custom_components.codex_assist.{module}.get_async_client", lambda hass: client,
            )
        yield events, requests


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
async def test_incomplete_text_is_reported_as_failure(hass, task_entry, stream_backend, surface):
    events, requests = stream_backend
    events.append(TEXT)
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Give a test answer.", None, Context(), agent_id="conversation.codex_assist",
        )
        assert "before response.completed" in result.response.speech["plain"]["speech"]
    else:
        with pytest.raises(HomeAssistantError, match="before response.completed"):
            await ai_task.async_generate_data(
                hass, task_name="Test", instructions="Give a test answer.",
                entity_id="ai_task.codex_assist_ai_task",
            )
    assert requests == ["/backend-api/codex/responses"]


@pytest.mark.parametrize("preview", [False, True])
async def test_incomplete_image_is_not_persisted(
    hass, task_entry, stream_backend, tmp_path, preview,
):
    events, requests = stream_backend
    encoded = base64.b64encode(PNG_BYTES).decode()
    events.append({
        "type": "response.image_generation_call.partial_image", "partial_image_b64": encoded,
    } if preview else {
        "type": "response.output_item.done",
        "item": {"type": "image_generation_call", "result": encoded},
    })
    with pytest.raises(HomeAssistantError, match="before response.completed"):
        await hass.services.async_call("ai_task", "generate_image", {
            "task_name": "Test", "instructions": "Draw a test image.",
            "entity_id": "ai_task.codex_assist_ai_task",
        }, blocking=True, return_response=True)
    assert requests == ["/backend-api/codex/responses"]
    assert list((tmp_path / "ai_task" / "image").glob("*")) == []


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
@pytest.mark.parametrize("visible", [False, True])
async def test_auth_retries_only_before_visible_output(
    hass, task_entry, stream_backend, surface, visible,
):
    events, requests = stream_backend
    events.extend([TEXT, AUTH_FAILURE] if visible else [AUTH_FAILURE])
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Give a test answer.", None, Context(), agent_id="conversation.codex_assist",
        )
        speech = result.response.speech["plain"]["speech"]
        if visible:
            assert "authentication failed" in speech
        else:
            assert speech == "A complete answer."
    elif visible:
        with pytest.raises(HomeAssistantError, match="authentication failed"):
            await ai_task.async_generate_data(
                hass, task_name="Test", instructions="Give a test answer.",
                entity_id="ai_task.codex_assist_ai_task",
            )
    else:
        result = await ai_task.async_generate_data(
            hass, task_name="Test", instructions="Give a test answer.",
            entity_id="ai_task.codex_assist_ai_task",
        )
        assert result.data == "A complete answer."
    assert requests == (["/backend-api/codex/responses"] if visible else [
        "/backend-api/codex/responses", "/oauth/token", "/backend-api/codex/responses",
    ])


@pytest.mark.parametrize("complete", [False, True])
async def test_native_tool_executes_only_after_completion(
    hass, task_entry, stream_backend, monkeypatch, complete,
):
    executed = []

    class Reading(llm.Tool):
        name = "HassTestReading"
        description = "Return a synthetic reading."
        parameters = vol.Schema({})

        async def async_call(self, hass, tool_input, llm_context):
            executed.append(tool_input.id)
            data = {"reading": 7}
            return llm.ToolResult(data=data) if hasattr(llm, "ToolResult") else data

    class ReadingAPI(llm.API):
        async def async_get_api_instance(self, llm_context):
            return llm.APIInstance(
                api=self, api_prompt="A harmless test reading is available.",
                llm_context=llm_context, tools=[Reading()],
            )

    async def get_api(hass, api_id, llm_context):
        api = ReadingAPI(hass=hass, id="stream_test", name="Stream test")
        return await api.async_get_api_instance(llm_context)

    monkeypatch.setattr(llm, "async_get_api", get_api)
    events, requests = stream_backend
    events.append({"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "reading-1", "name": "HassTestReading",
        "arguments": "{}", "status": "completed",
    }})
    if complete:
        events.append(COMPLETED)
    result = await conversation.async_converse(
        hass, "Get the test reading.", None, Context(), agent_id="conversation.codex_assist",
    )
    assert executed == (["reading-1"] if complete else [])
    speech = result.response.speech["plain"]["speech"]
    if complete:
        assert speech == "A complete answer."
    else:
        assert "before response.completed" in speech
    assert requests == ["/backend-api/codex/responses"] * (2 if complete else 1)
