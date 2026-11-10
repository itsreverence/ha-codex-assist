"""Outbound guidance and native HA routing with scripted HTTPX responses.

The response script does not inspect instructions or simulate model compliance.
These tests cannot establish live model behavior or satellite microphone state.
"""

from copy import deepcopy

import pytest
from homeassistant.components import ai_task, conversation
from homeassistant.components.conversation.chat_log import async_subscribe_chat_logs
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_web_search_delivery import (
    COMPLETED,
    call_event,
    message,
    model_round,
    search_reply,
    text_events,
)
from test_web_search_delivery import backend as backend

from custom_components.codex_assist import DOMAIN


@pytest.fixture
def entry_settings():
    return {}, {}


@pytest.fixture
def web_search():
    return False


@pytest.fixture
async def clarification_entry(hass, entry_settings, web_search):
    assert await async_setup_component(hass, "homeassistant", {})
    data, options = entry_settings
    entry = MockConfigEntry(
        domain=DOMAIN, title="Codex Assist", unique_id=DOMAIN,
        data={"access_token": "test-access", "refresh_token": "test-refresh",
              "model": "gpt-5.4", **data},
        options={"web_search": web_search, **options},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def assert_clarification_guidance(instructions):
    for requirement in (
        "When clarification is required, ask one concise, direct question",
        "as the final sentence",
        "appropriate question mark for the user's language and nothing after it",
        "Resolve ambiguous actions or targets before executing them",
        "Do not add unnecessary closing questions after ordinary answers, "
        "completed actions, or cancellation",
    ):
        assert instructions.count(requirement) == 1


def assert_search_speech_guidance(instructions, enabled):
    for requirement in (
        "do not include raw URLs, markdown links, or a Source/Sources section",
        "Refer to sources by human-readable names only.",
    ):
        assert instructions.count(requirement) == int(enabled)
    assert "renders structured citations separately" not in instructions


@pytest.mark.parametrize("web_search", [False, True])
@pytest.mark.parametrize(("entry_settings", "expected", "absent"), [
    (({}, {}), "You are a concise Home Assistant Assist conversation agent.", None),
    (({"prompt": "Use short replies."}, {}), "Use short replies.", None),
    (({"prompt": "Old data prompt."}, {"prompt": "Options prompt."}),
     "Options prompt.", "Old data prompt."),
    (({}, {"prompt": "Template language {{ llm_context.language }}."}),
     "Template language el.", "{{"),
])
async def test_outbound_clarification_guidance_preserves_ha_prompt(
    hass, clarification_entry, backend, request, expected, absent, web_search,
):
    planned, requests = backend
    stored = deepcopy((dict(clarification_entry.data), dict(clarification_entry.options)))
    chat_logs = []
    request.addfinalizer(async_subscribe_chat_logs(
        hass, lambda conversation_id, event_type, data: (
            chat_logs.append(data["chat_log"]) if "chat_log" in data else None
        ),
    ))
    conversation_id = None
    for _ in range(2):
        model_round(planned, text_events("A plain answer.") + [COMPLETED])
        result = await conversation.async_converse(
            hass, "Give a test answer.", conversation_id, Context(), language="el",
            agent_id="conversation.codex_assist",
            extra_system_prompt="Extra pipeline prompt." if conversation_id is None else None,
        )
        conversation_id = result.conversation_id
        system = chat_logs[-1]["content"][0]["content"]
        instructions = requests[-1][1]["instructions"]
        assert expected in system
        assert "Extra pipeline prompt." in system
        assert "When clarification is required" not in system
        assert_clarification_guidance(instructions)
        assert instructions.startswith(system + "\n\n")
        if absent is not None:
            assert absent not in instructions
        assert_search_speech_guidance(instructions, web_search)
    assert (dict(clarification_entry.data), dict(clarification_entry.options)) == stored
    assert len(requests) == 2


@pytest.mark.parametrize(("reply", "language", "continues"), [
    ("Which light?", "en", True),
    ("Ποιο φως;", "el", True),
    ("どのライトですか？", "ja", True),
    ("The temperature is twenty degrees.", "en", False),
    ("The light is on.", "en", False),
    ("Cancelled.", "en", False),
    ("Did you mean the first light...", "en", False),
    ("Did you mean the first light.", "en", False),
    ("Which light? I will wait.", "en", False),
    ('"Which light?"', "en", False),
    ("أي ضوء؟", "ar", False),
])
async def test_native_ha_continuation_keeps_scripted_text_and_history(
    hass, clarification_entry, backend, reply, language, continues,
):
    planned, requests = backend
    model_round(planned, text_events(reply) + [COMPLETED])
    result = await conversation.async_converse(
        hass, "Give a test response.", None, Context(), language=language,
        agent_id="conversation.codex_assist",
    )
    assert result.response.speech["plain"]["speech"] == reply
    assert result.continue_conversation is continues
    assert result.conversation_id
    if continues:
        model_round(planned, text_events("Understood.") + [COMPLETED])
        followup = await conversation.async_converse(
            hass, "The first one.", result.conversation_id, Context(), language=language,
            agent_id="conversation.codex_assist",
        )
        assert followup.conversation_id == result.conversation_id
        assert followup.continue_conversation is False
        assert requests[-1][1]["input"] == [
            {"role": "user", "content": "Give a test response."},
            message(reply),
            {"role": "user", "content": "The first one."},
        ]


@pytest.mark.parametrize("web_search", [True])
async def test_recovered_question_keeps_native_continuation_without_source_card(
    hass, clarification_entry, backend,
):
    planned, requests = backend
    model_round(planned, [call_event("search-1", "web_search", {"queries": ["synthetic"]}),
                          COMPLETED])
    search_reply(planned)
    question = "Which documentation page?"
    model_round(planned, [
        {"type": "response.output_item.done", "item": message(question)}, COMPLETED,
    ])
    result = await conversation.async_converse(
        hass, "Find the documentation.", None, Context(), agent_id="conversation.codex_assist",
    )
    assert result.response.speech["plain"]["speech"] == question
    assert result.continue_conversation is True
    assert not result.response.card
    for path, payload in requests:
        if path.endswith("/responses"):
            assert_clarification_guidance(payload["instructions"])
            assert_search_speech_guidance(payload["instructions"], True)


@pytest.mark.parametrize("web_search", [False, True])
async def test_clarification_then_native_action_preserves_guidance_through_retry_and_handoff(
    hass, clarification_entry, backend, web_search,
):
    """A fixed script proves routing only, not that the model waits for clarification."""
    executed = []

    async def turn_on(call):
        executed.append(call.data)

    hass.services.async_register("light", "turn_on", turn_on)
    for name in ("alpha", "beta"):
        entity_id = f"light.test_{name}"
        hass.states.async_set(entity_id, "off", {"friendly_name": f"Test {name}"})
        async_expose_entity(hass, "conversation", entity_id, True)

    planned, requests = backend
    planned.append(("/backend-api/codex/responses", 401, {"error": "Synthetic rejection"}))
    planned.append(("/oauth/token", 200, {
        "access_token": "test-new-access", "refresh_token": "test-new-refresh",
    }))
    question = "Which light, Test alpha or Test beta?"
    model_round(planned, text_events(question) + [COMPLETED])
    result = await conversation.async_converse(
        hass, "Turn on the test light.", None, Context(), agent_id="conversation.codex_assist",
    )
    assert result.response.speech["plain"]["speech"] == question
    assert result.continue_conversation is True
    assert executed == []

    turn_on_tool = next(
        tool["name"] for tool in requests[-1][1]["tools"]
        if tool["name"].endswith("HassTurnOn")
    )
    model_round(planned, [call_event("action-1", turn_on_tool, {
        "name": "Test alpha", "domain": ["light"],
    }), COMPLETED])
    model_round(planned, text_events("Test alpha is on.") + [COMPLETED])
    followup = await conversation.async_converse(
        hass, "Test alpha.", result.conversation_id, Context(),
        agent_id="conversation.codex_assist",
    )
    assert followup.conversation_id == result.conversation_id
    assert followup.continue_conversation is False
    assert followup.response.speech["plain"]["speech"] == "Test alpha is on."
    assert len(executed) == 1, requests[-1][1]["input"][-1]
    assert executed[0]["entity_id"] == ["light.test_alpha"]
    model_requests = [payload for path, payload in requests if path.endswith("/responses")]
    assert len(model_requests) == 4
    for payload in model_requests:
        assert_clarification_guidance(payload["instructions"])
        assert_search_speech_guidance(payload["instructions"], web_search)
    assert model_requests[0]["instructions"] == model_requests[1]["instructions"]
    assert model_requests[2]["instructions"] == model_requests[3]["instructions"]
    assert model_requests[2]["input"] == [
        {"role": "user", "content": "Turn on the test light."},
        message(question),
        {"role": "user", "content": "Test alpha."},
    ]
    assert model_requests[3]["input"][-1]["type"] == "function_call_output"
    assert model_requests[3]["input"][-1]["call_id"] == "action-1"


@pytest.mark.parametrize("web_search", [False, True])
async def test_ai_task_does_not_receive_conversation_clarification_guidance(
    hass, clarification_entry, backend,
):
    planned, requests = backend
    model_round(planned, text_events("A task answer.") + [COMPLETED])
    result = await ai_task.async_generate_data(
        hass, task_name="Test", instructions="Summarize the task.",
        entity_id="ai_task.codex_assist_ai_task",
    )
    assert result.data == "A task answer."
    assert len(requests) == 1
    instructions = requests[0][1]["instructions"]
    assert instructions
    assert requests[0][1]["input"] == [{"role": "user", "content": "Summarize the task."}]
    assert "When clarification is required" not in instructions
    assert "Resolve ambiguous actions or targets" not in instructions
