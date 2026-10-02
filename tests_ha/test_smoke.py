"""Smoke tests against a real Home Assistant instance.

These verify the integration wires into real HA APIs (config entries,
conversation platform, AI Task platform, chat log streaming) instead of the
lightweight fakes used by the main test suite. Only the Codex backend HTTP
calls are stubbed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.components import conversation
from homeassistant.components.conversation import trace as conversation_trace
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import llm, selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_assist import DOMAIN
from custom_components.codex_assist.ai_task import (
    _structured_data_from_text,
    _structured_output_format,
)
from custom_components.codex_assist.codex_client import (
    CodexCitation,
    CodexCitationDelta,
    CodexClient,
    CodexResponseItemDelta,
    CodexTextDelta,
    CodexToolCall,
    CodexToolCallDelta,
)
from custom_components.codex_assist.config_flow import (
    CONF_PROMPT,
    SECTION_ADVANCED_SETTINGS,
    SECTION_CHAT_SETTINGS,
    SECTION_IMAGE_SETTINGS,
)
from custom_components.codex_assist.diagnostics import (
    REDACTED,
    async_get_config_entry_diagnostics,
)

ENTRY_DATA = {
    # Not a JWT, so the runtime treats it as non-expiring and skips refresh.
    "access_token": "test-access-token",
    "refresh_token": "test-refresh-token",
    "model": "gpt-5.4",
    "prompt": "You are a concise Home Assistant Assist conversation agent.",
}


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Codex Assist",
        unique_id=DOMAIN,
        data=dict(ENTRY_DATA),
    )


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    # The conversation component requires the core homeassistant component
    # (exposed-entities registry) to be set up first.
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_creates_conversation_and_ai_task_entities(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("conversation.codex_assist") is not None
    assert hass.states.get("ai_task.codex_assist_ai_task") is not None


async def test_unload_entry_cleans_up(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_conversation_turn_streams_codex_reply(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _setup_entry(hass)

    async def fake_stream_turn(self: CodexClient, **kwargs: object):
        tools = kwargs["tools"]
        assert isinstance(tools, list)
        function_tools = [tool for tool in tools if tool.get("type") == "function"]
        assert function_tools
        assert all(isinstance(tool.get("parameters"), dict) for tool in function_tools)
        yield CodexTextDelta("The porch light is on.")

    monkeypatch.setattr(CodexClient, "stream_turn", fake_stream_turn)

    result = await conversation.async_converse(
        hass,
        "Is the porch light on?",
        None,
        Context(),
        agent_id="conversation.codex_assist",
    )

    speech = result.response.speech["plain"]["speech"]
    assert speech == "The porch light is on."


async def test_conversation_serializes_ha_state_tool_result(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay a State-valued tool result through HA's real conversation loop."""
    await _setup_entry(hass)
    hass.states.async_set("lock.test_door", "locked", {"friendly_name": "Test door"})
    state = hass.states.get("lock.test_door")
    assert state is not None
    tool_calls = []

    class FakeApiInstance:
        api_prompt = "A read-only test tool is available."
        tools = [
            SimpleNamespace(
                name="HassTestState", description="Read a test state", parameters=vol.Schema({})
            )
        ]
        custom_serializer = None

        async def async_call_tool(self, tool_input: llm.ToolInput):
            tool_calls.append(tool_input)
            return {"state": state}

    async def fake_get_api(*args: object, **kwargs: object):
        return FakeApiInstance()

    monkeypatch.setattr(llm, "async_get_api", fake_get_api)
    calls = []

    async def fake_stream_turn(self: CodexClient, **kwargs: object):
        calls.append(kwargs["input_items"])
        if len(calls) == 1:
            yield CodexToolCallDelta(
                CodexToolCall(id="state-query-1", name="HassTestState", arguments={})
            )
            return
        yield CodexTextDelta("The test door is locked.")

    monkeypatch.setattr(CodexClient, "stream_turn", fake_stream_turn)

    result = await conversation.async_converse(
        hass,
        "Is the test door locked?",
        None,
        Context(),
        agent_id="conversation.codex_assist",
    )

    assert result.response.speech["plain"]["speech"] == "The test door is locked."
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "HassTestState"
    assert len(calls) == 2
    tool_outputs = [item for item in calls[1] if item.get("type") == "function_call_output"]
    assert len(tool_outputs) == 1
    assert tool_outputs[0]["call_id"] == "state-query-1"
    output = json.loads(tool_outputs[0]["output"])
    assert output["state"]["entity_id"] == state.entity_id
    assert output["state"]["state"] == "locked"
    assert output["state"]["attributes"] == {"friendly_name": "Test door"}
    assert output["state"]["last_changed"] == state.last_changed.isoformat()


@pytest.mark.parametrize("content_type", ["tool_result", "tool_args"])
async def test_chat_log_serializes_timestamps_with_real_ha_helper(
    hass: HomeAssistant,
    content_type: str,
) -> None:
    from custom_components.codex_assist.conversation import _codex_input_from_chat_log

    payload = {"updated_at": datetime(2026, 9, 30, 12, 0, tzinfo=UTC), "count": 1}
    if content_type == "tool_result":
        content = conversation.ToolResultContent(
            agent_id="conversation.codex_assist",
            tool_call_id="call-1",
            tool_name="HassTestTool",
            tool_result=payload,
        )
        field = "output"
    else:
        content = conversation.AssistantContent(
            agent_id="conversation.codex_assist",
            tool_calls=[llm.ToolInput(id="call-1", tool_name="HassTestTool", tool_args=payload)],
        )
        field = "arguments"

    items = await _codex_input_from_chat_log(hass, SimpleNamespace(content=[content]))

    assert len(items) == 1
    assert items[0]["call_id"] == "call-1"
    assert json.loads(items[0][field]) == {"updated_at": "2026-09-30T12:00:00+00:00", "count": 1}


async def test_conversation_replays_native_codex_output_on_next_turn(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_trace.async_clear_traces()
    await _setup_entry(hass)
    caplog.set_level(logging.DEBUG, logger=conversation.ChatLog.__module__)
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "encrypted_content": "encrypted-state",
        "summary": [],
    }
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "First reply.", "annotations": []}],
    }
    calls: list[list[dict[str, object]]] = []

    async def fake_stream_turn(self: CodexClient, **kwargs: object):
        input_items = kwargs["input_items"]
        assert isinstance(input_items, list)
        calls.append(input_items)
        if len(calls) == 1:
            yield CodexTextDelta("First reply.")
            yield CodexResponseItemDelta(reasoning)
            yield CodexResponseItemDelta(message)
            return
        yield CodexTextDelta("Second reply.")

    monkeypatch.setattr(CodexClient, "stream_turn", fake_stream_turn)

    first = await conversation.async_converse(
        hass,
        "First question",
        None,
        Context(),
        agent_id="conversation.codex_assist",
    )
    second = await conversation.async_converse(
        hass,
        "Second question",
        first.conversation_id,
        Context(),
        agent_id="conversation.codex_assist",
    )

    assert second.response.speech["plain"]["speech"] == "Second reply."
    assert calls[1] == [
        {"role": "user", "content": "First question"},
        reasoning,
        message,
        {"role": "user", "content": "Second question"},
    ]
    assert "encrypted-state" not in caplog.text
    assert "CodexNativeState(item_count=2)" in caplog.text
    serialized_traces = [trace.as_dict() for trace in conversation_trace.async_get_traces()]
    serialized_trace_text = str(serialized_traces)
    assert "encrypted-state" not in serialized_trace_text
    assert "'redacted': True" in serialized_trace_text
    assert "'item_count': 2" in serialized_trace_text


async def test_web_search_citations_are_displayable_but_not_spoken(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _setup_entry(hass)
    hass.config_entries.async_update_entry(entry, options={"web_search": True})

    async def fake_stream_turn(self: CodexClient, **kwargs: object):
        tools = kwargs["tools"]
        assert isinstance(tools, list)
        assert {"type": "web_search"} in tools
        yield CodexTextDelta("IANA maintains the reserved domains.")
        yield CodexCitationDelta(
            CodexCitation(
                title="IANA Reserved Domains",
                url="https://www.iana.org/help/example-domains",
                start_index=0,
                end_index=4,
            )
        )

    monkeypatch.setattr(CodexClient, "stream_turn", fake_stream_turn)

    result = await conversation.async_converse(
        hass,
        "Who maintains the reserved domains?",
        None,
        Context(),
        agent_id="conversation.codex_assist",
    )

    assert result.response.speech["plain"]["speech"] == ("IANA maintains the reserved domains.")
    assert result.response.card["simple"] == {
        "title": "Sources",
        "content": ("- IANA Reserved Domains — <https://www.iana.org/help/example-domains>"),
    }


async def test_multi_turn_web_search_sources_are_not_spoken_after_ha_tool_call(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _setup_entry(hass)
    hass.config_entries.async_update_entry(entry, options={"web_search": True})

    fake_tool = SimpleNamespace(
        name="HassTestTool",
        description="A harmless test tool",
        parameters=vol.Schema({}),
    )

    class FakeApiInstance:
        api_prompt = "A harmless test tool is available."
        tools = [fake_tool]
        custom_serializer = None

        async def async_call_tool(self, tool_input: llm.ToolInput) -> dict[str, bool]:
            assert tool_input.tool_name == "HassTestTool"
            return {"success": True}

    async def fake_get_api(*args: object, **kwargs: object) -> FakeApiInstance:
        return FakeApiInstance()

    monkeypatch.setattr(llm, "async_get_api", fake_get_api)

    call_count = 0

    async def fake_stream_turn(self: CodexClient, **kwargs: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield CodexTextDelta("I found the background information.")
            yield CodexCitationDelta(
                CodexCitation(
                    title="Background source",
                    url="https://example.com/background",
                    start_index=0,
                    end_index=4,
                )
            )
            yield CodexToolCallDelta(
                CodexToolCall(
                    id="call-1",
                    name="HassTestTool",
                    arguments={},
                )
            )
            return
        yield CodexTextDelta("The harmless tool completed successfully.")
        yield CodexCitationDelta(
            CodexCitation(
                title="Final source",
                url="https://example.com/final",
                start_index=0,
                end_index=4,
            )
        )

    monkeypatch.setattr(CodexClient, "stream_turn", fake_stream_turn)

    result = await conversation.async_converse(
        hass,
        "Research this and use the harmless tool.",
        None,
        Context(),
        agent_id="conversation.codex_assist",
    )

    assert call_count == 2
    assert result.response.speech["plain"]["speech"] == (
        "The harmless tool completed successfully."
    )
    assert result.response.card["simple"] == {
        "title": "Sources",
        "content": (
            "- Background source — <https://example.com/background>\n"
            "- Final source — <https://example.com/final>"
        ),
    }


async def test_diagnostics_redact_tokens_on_real_entry(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    entry_data = diagnostics["entry"]["data"]
    assert entry_data["access_token"] == REDACTED
    assert entry_data["refresh_token"] == REDACTED
    assert entry_data["model"] == "gpt-5.4"
    assert "test-access-token" not in str(diagnostics)


async def test_options_flow_uses_real_home_assistant_contract(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await _setup_entry(hass)

    from custom_components.codex_assist.codex_models import ModelCatalog

    async def fake_model_ids(*args, **kwargs):
        return ModelCatalog(("gpt-5.4",), "discovered")

    monkeypatch.setattr(
        "custom_components.codex_assist.config_flow.async_entry_model_catalog",
        fake_model_ids,
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert [key.schema for key in result["data_schema"].schema] == [
        SECTION_CHAT_SETTINGS,
        SECTION_ADVANCED_SETTINGS,
        SECTION_IMAGE_SETTINGS,
    ]
    advanced_section = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key.schema == SECTION_ADVANCED_SETTINGS
    )
    prompt_selector = next(
        value for key, value in advanced_section.schema.schema.items() if key.schema == CONF_PROMPT
    )
    assert isinstance(prompt_selector, selector.TextSelector)
    assert prompt_selector.config["multiline"] is True
    prompt = "# House rules\n\n- **Never** unlock doors.\n- Reply concisely."

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            SECTION_CHAT_SETTINGS: {
                "model": "gpt-5.4",
                "text_verbosity": "low",
                "web_search": True,
            },
            SECTION_ADVANCED_SETTINGS: {
                "prompt": prompt,
                "reasoning_effort": "low",
            },
            SECTION_IMAGE_SETTINGS: {
                "image_model": "gpt-image-2-medium",
                "image_size": "1024x1024",
            },
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["text_verbosity"] == "low"
    assert result["data"]["web_search"] is True
    assert entry.options["prompt"] == prompt


def test_structured_output_uses_real_home_assistant_schema_converter() -> None:
    task = SimpleNamespace(
        name="Porch state",
        structure=vol.Schema({vol.Required("state"): vol.In(["on", "off"])}),
    )
    chat_log = SimpleNamespace(llm_api=None)

    text_format = _structured_output_format(task, chat_log)

    assert text_format is not None
    assert text_format["type"] == "json_schema"
    assert text_format["name"] == "porch_state"
    assert text_format["schema"]["required"] == ["state"]
    assert text_format["schema"]["properties"]["state"]["enum"] == ["on", "off"]


def test_structured_output_serializes_text_selector_for_strict_codex_schema() -> None:
    task = SimpleNamespace(
        name="Home comfort",
        structure=vol.Schema(
            {
                vol.Required("summary"): selector.TextSelector(),
                vol.Optional("note"): selector.TextSelector(),
            }
        ),
    )
    chat_log = SimpleNamespace(llm_api=None)

    text_format = _structured_output_format(task, chat_log)

    assert text_format is not None
    assert text_format["strict"] is True
    assert text_format["schema"] == {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "note": {"type": ["string", "null"]},
        },
        "required": ["summary", "note"],
        "additionalProperties": False,
    }


def test_structured_output_preserves_composition_and_nullable_semantics() -> None:
    structure = vol.Schema(
        {
            vol.Required("nullable"): vol.Any(str, None),
            vol.Required("choices"): [
                vol.Any(
                    {
                        vol.Required("kind"): "a",
                        vol.Optional("note", default="default note"): str,
                    },
                    {
                        vol.Required("kind"): "b",
                        vol.Optional("count"): int,
                    },
                )
            ],
        }
    )
    task = SimpleNamespace(name="Composed output", structure=structure)

    text_format = _structured_output_format(task, SimpleNamespace(llm_api=None))

    assert text_format is not None
    nullable_schema = text_format["schema"]["properties"]["nullable"]
    assert nullable_schema["type"] == ["string", "null"]
    assert "nullable" not in nullable_schema
    assert _structured_data_from_text(
        '{"nullable":null,"choices":[{"kind":"a","note":null}]}', structure
    ) == {
        "nullable": None,
        "choices": [{"kind": "a", "note": "default note"}],
    }


def test_structured_output_restores_composed_key_placeholders() -> None:
    structure = vol.Schema({vol.Required(vol.Any("email", "phone")): str})
    task = SimpleNamespace(name="Contact output", structure=structure)

    text_format = _structured_output_format(task, SimpleNamespace(llm_api=None))

    assert text_format is not None
    assert _structured_data_from_text('{"email":"person@example.com","phone":null}', structure) == {
        "email": "person@example.com"
    }


def test_structured_output_restores_optional_defaults_inside_all() -> None:
    structure = vol.Schema(
        {
            vol.Required("payload"): vol.All(
                {
                    vol.Required("name"): str,
                    vol.Optional("note", default="fallback"): str,
                }
            )
        }
    )
    task = SimpleNamespace(name="All output", structure=structure)

    text_format = _structured_output_format(task, SimpleNamespace(llm_api=None))

    assert text_format is not None
    note_schema = text_format["schema"]["properties"]["payload"]["properties"]["note"]
    assert note_schema["type"] == ["string", "null"]
    assert _structured_data_from_text('{"payload":{"name":"x","note":null}}', structure) == {
        "payload": {"name": "x", "note": "fallback"}
    }


async def test_new_setup_discovers_models_after_owned_sign_in(hass, monkeypatch):
    from unittest.mock import AsyncMock

    from custom_components.codex_assist.codex_auth import (
        CodexAuthorizationCode,
        CodexDeviceCode,
        CodexTokenSet,
    )

    auth = SimpleNamespace(
        request_device_code=AsyncMock(
            return_value=CodexDeviceCode(
                user_code="TEST-CODE",
                device_auth_id="test-device",
                interval=5,
                verification_uri="https://auth.openai.com/codex/device",
            )
        ),
        poll_device_code=AsyncMock(return_value=CodexAuthorizationCode("test-code", "verifier")),
        exchange_authorization_code=AsyncMock(
            return_value=CodexTokenSet("test-access", "test-refresh")
        ),
    )
    monkeypatch.setattr(
        "custom_components.codex_assist.config_flow.CodexAssistConfigFlow._auth_client",
        lambda self: auth,
    )
    discover = AsyncMock(return_value=["future-model", "gpt-5.6-terra"])
    monkeypatch.setattr(
        "custom_components.codex_assist.config_flow.fetch_codex_model_ids", discover
    )
    result = await hass.config_entries.flow.async_init("codex_assist", context={"source": "user"})
    assert result["step_id"] == "user"
    assert not result["data_schema"].schema
    discover.assert_not_awaited()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result["step_id"] == "device"
    discover.assert_not_awaited()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result["step_id"] == "model"
    model_field, model_selector = next(iter(result["data_schema"].schema.items()))
    assert model_field.default() == "future-model"
    assert [o["value"] for o in model_selector.config["options"]] == [
        "future-model",
        "gpt-5.6-terra",
    ]
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"model": "invented-model"}
        )
    monkeypatch.setattr(
        "custom_components.codex_assist.async_setup_entry", AsyncMock(return_value=True)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"model": "future-model"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["model"] == "future-model"
    await hass.async_block_till_done()


async def test_options_preserves_saved_model_when_account_list_changes(hass, monkeypatch):
    from custom_components.codex_assist.codex_models import ModelCatalog

    entry = await _setup_entry(hass)

    async def catalog(*args, **kwargs):
        return ModelCatalog(("new-model",), "discovered")

    monkeypatch.setattr(
        "custom_components.codex_assist.config_flow.async_entry_model_catalog", catalog
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    section = next(
        v for k, v in result["data_schema"].schema.items() if k.schema == SECTION_CHAT_SETTINGS
    )
    field, select = next((k, v) for k, v in section.schema.schema.items() if k.schema == "model")
    assert field.default() == entry.data["model"]
    assert select.config["options"][-1]["label"].endswith("(saved; not currently listed)")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            SECTION_CHAT_SETTINGS: {"text_verbosity": "low"},
            SECTION_ADVANCED_SETTINGS: {},
            SECTION_IMAGE_SETTINGS: {},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["model"] == entry.data["model"]


async def test_periodic_discovery_is_cancelled_when_entry_unloads(hass, monkeypatch):
    from datetime import timedelta
    from unittest.mock import AsyncMock, Mock

    cancel = Mock()
    callbacks = []

    def track(hass_arg, callback, interval):
        assert hass_arg is hass
        assert interval == timedelta(hours=6)
        callbacks.append(callback)
        return cancel

    refresh = AsyncMock()
    monkeypatch.setattr("homeassistant.helpers.event.async_track_time_interval", track)
    monkeypatch.setattr(
        "custom_components.codex_assist.model_discovery.async_entry_model_catalog", refresh
    )
    entry = await _setup_entry(hass)
    assert len(callbacks) == 1
    refresh.assert_not_awaited()
    await callbacks[0](None)
    refresh.assert_awaited_once_with(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    cancel.assert_called_once()
