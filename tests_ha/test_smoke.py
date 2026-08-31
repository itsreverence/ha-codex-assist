"""Smoke tests against a real Home Assistant instance.

These verify the integration wires into real HA APIs (config entries,
conversation platform, AI Task platform, chat log streaming) instead of the
lightweight fakes used by the main test suite. Only the Codex backend HTTP
calls are stubbed.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.components import conversation
from homeassistant.components.conversation import trace as conversation_trace
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import llm, selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_assist import DOMAIN
from custom_components.codex_assist.ai_task import _structured_output_format
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
        "content": [
            {"type": "output_text", "text": "First reply.", "annotations": []}
        ],
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

    async def fake_model_ids(**kwargs: object) -> list[str]:
        return ["gpt-5.4"]

    monkeypatch.setattr(
        "custom_components.codex_assist.config_flow.fetch_codex_model_ids",
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
        value
        for key, value in advanced_section.schema.schema.items()
        if key.schema == CONF_PROMPT
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
