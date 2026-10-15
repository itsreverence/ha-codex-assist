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
ANSWER = {"type": "response.output_item.done", "item": {
    "type": "message", "role": "assistant", "id": "answer",
    "content": [{"type": "output_text", "text": "The final answer."}],
}}


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
@pytest.mark.parametrize("prefix", [None, "The fi", "The final answer."])
async def test_completed_message_delivers_full_answer(
    hass, task_entry, stream_backend, surface, prefix,
):
    events, requests = stream_backend
    if prefix is not None:
        events.append({"type": "response.output_text.delta", "delta": prefix})
    events.extend([ANSWER, COMPLETED])
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Give a test answer.", None, Context(), agent_id="conversation.codex_assist",
        )
        assert result.response.speech["plain"]["speech"] == "The final answer."
    else:
        result = await ai_task.async_generate_data(
            hass, task_name="Test", instructions="Give a test answer.",
            entity_id="ai_task.codex_assist_ai_task",
        )
        assert result.data == "The final answer."
    assert requests == ["/backend-api/codex/responses"]


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
@pytest.mark.parametrize("event", ["done", "added"])
async def test_late_index_alias_rejects_reordered_answer(
    hass, task_entry, stream_backend, surface, event,
):
    events, requests = stream_backend
    later = {**ANSWER["item"], "id": "b", "content": [{"type": "output_text", "text": "Later"}]}
    earlier = {
        **ANSWER["item"], "id": "a", "content": [{"type": "output_text", "text": "Earlier"}],
    }
    events.extend([
        {"type": "response.output_item.done", "item": later},
        {"type": "response.output_item.done", "item": earlier, "output_index": 0},
        {"type": f"response.output_item.{event}", "item": later, "output_index": 1},
        COMPLETED,
    ])
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Give a test answer.", None, Context(), agent_id="conversation.codex_assist",
        )
        speech = result.response.speech["plain"]["speech"]
        assert "Codex returned inconsistent answer text" in speech
        assert not result.response.card
    else:
        with pytest.raises(HomeAssistantError, match="Codex returned inconsistent answer text"):
            await ai_task.async_generate_data(
                hass, task_name="Test", instructions="Give a test answer.",
                entity_id="ai_task.codex_assist_ai_task",
            )
    assert requests == ["/backend-api/codex/responses"]


def text_delta(text, item_id, content_index=0, **identity):
    return {
        "type": "response.output_text.delta", "delta": text, "item_id": item_id,
        "content_index": content_index, **identity,
    }


def completed_message(*parts, item_id, output_index):
    return {"type": "response.output_item.done", "item": {
        "type": "message", "role": "assistant", "id": item_id,
        "content": [{"type": "output_text", "text": text} for text in parts],
    }, **({"output_index": output_index} if output_index is not None else {})}


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
@pytest.mark.parametrize("output,expected", [
    pytest.param([
        text_delta("L", "b"), text_delta("E", "a", output_index=0), text_delta("l", "b"),
        completed_message("Ll", item_id="b", output_index=1),
        completed_message("E", item_id="a", output_index=0),
    ], None, id="interleaved-late-index"),
    pytest.param([
        text_delta("Later", "a", content_index=1, output_index=0),
        text_delta("Earlier", "a", content_index=0, output_index=0),
        completed_message("Earlier", "Later", item_id="a", output_index=0),
    ], None, id="reversed-full-parts"),
    pytest.param([
        text_delta("Ear", "a"), text_delta("lier", "a"),
        completed_message("Earlier", item_id="a", output_index=0),
    ], "Earlier", id="ordered-multichunk-part"),
    pytest.param([
        text_delta("Ear", "a"), text_delta("lier", "a"),
        text_delta("La", "a", content_index=1), text_delta("ter", "a", content_index=1),
        completed_message("Earlier", "Later", item_id="a", output_index=0),
    ], "EarlierLater", id="ordered-multichunk-parts"),
    *[
        pytest.param([
            text_delta("E", "a", **a), text_delta("X", "b", **b),
            text_delta("e", "a", content_index=last_index, **a),
            completed_message(
                *(["E", "e"] if last_index else ["Ee"]), item_id="a",
                output_index=a.get("output_index"),
            ),
            completed_message("X", item_id="b", output_index=b.get("output_index")),
        ], None, id=f"resumed-message-part{last_index}-{metadata}")
        for metadata, a, b in [
            ("full-index", {"output_index": 0}, {"output_index": 1}),
            ("only-a-index", {"output_index": 0}, {}),
            ("only-b-index", {}, {"output_index": 1}),
            ("no-index", {}, {}),
        ]
        for last_index in (0, 1)
    ],
    *[
        pytest.param([
            text_delta("E", "a", **a), text_delta("e", "a", **a),
            text_delta("X", "b", **b),
            completed_message("Ee", item_id="a", output_index=a.get("output_index")),
            completed_message("X", item_id="b", output_index=b.get("output_index")),
        ], "EeX", id=f"contiguous-messages-{metadata}")
        for metadata, a, b in [
            ("full-index", {"output_index": 0}, {"output_index": 1}),
            ("only-a-index", {"output_index": 0}, {}),
            ("only-b-index", {}, {"output_index": 1}),
            ("no-index", {}, {}),
        ]
    ],
    pytest.param([
        text_delta("E", "a", content_index=None), text_delta("X", "b"),
        text_delta("e", "a"),
        completed_message("Ee", item_id="a", output_index=None),
        completed_message("X", item_id="b", output_index=None),
    ], None, id="resumed-message-bound-content-index"),
    pytest.param([
        text_delta("E", "a", content_index=None), text_delta("X", "b"),
        completed_message("E", item_id="a", output_index=None),
        completed_message("X", item_id="b", output_index=None),
    ], "EX", id="contiguous-messages-bound-content-index"),
    pytest.param([
        *(text_delta("x", "a", content_index=None) for _ in range(2048)),
        completed_message("x" * 2048 + "!", item_id="a", output_index=0),
        completed_message("Next", item_id="b", output_index=None),
    ], "x" * 2048 + "!Next", id="contiguous-long-adjacent-chunks"),
])
async def test_delivered_order_controls_success_and_native_state(
    hass, task_entry, stream_backend, monkeypatch, surface, output, expected,
):
    from custom_components.codex_assist import ai_task as task_owner
    from custom_components.codex_assist import conversation as conversation_owner

    logs = []
    original = conversation_owner._stream_codex_turn_into_chat_log

    async def record(**kwargs):
        logs.append(kwargs["chat_log"])
        return await original(**kwargs)

    for owner in (task_owner, conversation_owner):
        monkeypatch.setattr(owner, "_stream_codex_turn_into_chat_log", record)
    events, requests = stream_backend
    events.extend([*output, COMPLETED])
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Give a test answer.", None, Context(), agent_id="conversation.codex_assist",
        )
        speech = result.response.speech["plain"]["speech"]
        if expected is None:
            assert "Codex returned inconsistent answer text" in speech
        else:
            assert speech == expected
        assert not result.response.card
    elif expected is None:
        with pytest.raises(HomeAssistantError, match="Codex returned inconsistent answer text"):
            await ai_task.async_generate_data(
                hass, task_name="Test", instructions="Give a test answer.",
                entity_id="ai_task.codex_assist_ai_task",
            )
    else:
        result = await ai_task.async_generate_data(
            hass, task_name="Test", instructions="Give a test answer.",
            entity_id="ai_task.codex_assist_ai_task",
        )
        assert result.data == expected
    assert requests == ["/backend-api/codex/responses"]
    assert len(logs) == 1
    assert any(getattr(content, "native", None) is not None for content in logs[0].content) == (
        expected is not None
    )


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
@pytest.mark.parametrize("output,expected", [
    *[
        pytest.param([
            text_delta("E", "a"), text_delta("X", "b"),
            {"type": "response.output_text.delta", "delta": "e"}, *ending,
        ], None, id=name)
        for name, ending in [
            ("delta-only-completion", [COMPLETED]),
            ("premature-eof", []),
            ("message-done", [completed_message("Ee", item_id="a", output_index=None),
                              COMPLETED]),
        ]
    ],
    pytest.param([
        text_delta("E", "a"), completed_message("E", item_id="a", output_index=None),
        text_delta("X", "b"), {"type": "response.output_text.delta", "delta": "e"}, COMPLETED,
    ], None, id="completed-message-before-anonymous"),
    pytest.param([
        text_delta("E", "a"), {"type": "response.output_text.delta", "delta": "e"},
        completed_message("Ee", item_id="a", output_index=None), COMPLETED,
    ], "Ee", id="single-message-anonymous"),
    pytest.param([
        text_delta("E", "a"), text_delta("X", "b"), text_delta("x", "b"),
        completed_message("E", item_id="a", output_index=None),
        completed_message("Xx", item_id="b", output_index=None), COMPLETED,
    ], "EXx", id="multiple-messages-identified"),
    pytest.param([
        text_delta("E", "a"), text_delta("X", "b"),
        {"type": "response.output_text.delta", "delta": ""},
        completed_message("E", item_id="a", output_index=None),
        completed_message("X", item_id="b", output_index=None), COMPLETED,
    ], "EX", id="multiple-messages-empty-anonymous"),
])
async def test_anonymous_delta_controls_native_commit_and_sources(
    hass, task_entry, stream_backend, monkeypatch, surface, output, expected,
):
    from custom_components.codex_assist import ai_task as task_owner
    from custom_components.codex_assist import conversation as conversation_owner

    logs = []
    original = conversation_owner._stream_codex_turn_into_chat_log

    async def record(**kwargs):
        logs.append(kwargs["chat_log"])
        return await original(**kwargs)

    for owner in (task_owner, conversation_owner):
        monkeypatch.setattr(owner, "_stream_codex_turn_into_chat_log", record)
    events, requests = stream_backend
    events.extend([
        {"type": "response.output_item.done", "item": {
            "type": "reasoning", "id": "reasoning", "encrypted_content": "synthetic",
        }},
        {"type": "response.output_text.annotation.added", "annotation": {
            "type": "url_citation", "title": "Example", "url": "https://example.com/docs",
        }},
        *output,
    ])
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Give a test answer.", None, Context(), agent_id="conversation.codex_assist",
        )
        speech = result.response.speech["plain"]["speech"]
        if expected is None:
            assert "Codex returned inconsistent answer text" in speech
            assert not result.response.card
        else:
            assert speech == expected
            assert result.response.card["simple"]["title"] == "Sources"
    elif expected is None:
        with pytest.raises(HomeAssistantError, match="Codex returned inconsistent answer text"):
            await ai_task.async_generate_data(
                hass, task_name="Test", instructions="Give a test answer.",
                entity_id="ai_task.codex_assist_ai_task",
            )
    else:
        result = await ai_task.async_generate_data(
            hass, task_name="Test", instructions="Give a test answer.",
            entity_id="ai_task.codex_assist_ai_task",
        )
        assert result.data == expected
    assert requests == ["/backend-api/codex/responses"]
    assert len(logs) == 1
    assert any(getattr(content, "native", None) is not None for content in logs[0].content) == (
        expected is not None
    )


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
@pytest.mark.parametrize("output", [
    [],
    [{"type": "response.output_text.delta", "delta": " \n"}],
    [{"type": "response.output_item.done", "item": {**ANSWER["item"], "phase": "commentary"}}],
])
async def test_silent_completed_response_is_failure(
    hass, task_entry, stream_backend, surface, output,
):
    events, requests = stream_backend
    events.extend([*output, COMPLETED])
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Give a test answer.", None, Context(), agent_id="conversation.codex_assist",
        )
        assert "Codex returned no answer text" in result.response.speech["plain"]["speech"]
        assert not result.response.card
    else:
        with pytest.raises(HomeAssistantError, match="Codex returned no answer text"):
            await ai_task.async_generate_data(
                hass, task_name="Test", instructions="Give a test answer.",
                entity_id="ai_task.codex_assist_ai_task",
            )
    assert requests == ["/backend-api/codex/responses"]


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
async def test_recovered_answer_prevents_auth_replay(hass, task_entry, stream_backend, surface):
    events, requests = stream_backend
    events.extend([ANSWER, AUTH_FAILURE])
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Give a test answer.", None, Context(), agent_id="conversation.codex_assist",
        )
        assert "authentication failed" in result.response.speech["plain"]["speech"]
        assert not result.response.card
    else:
        with pytest.raises(HomeAssistantError, match="authentication failed"):
            await ai_task.async_generate_data(
                hass, task_name="Test", instructions="Give a test answer.",
                entity_id="ai_task.codex_assist_ai_task",
            )
    assert requests == ["/backend-api/codex/responses"]


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
