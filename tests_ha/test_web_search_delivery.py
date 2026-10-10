"""Client-side search through real HA owners; only backend HTTP is substituted.

These tests prove delivery, replay and HA tool routing, not live Codex service
availability, authentication, device effects or model choice of sources.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from urllib.parse import parse_qs

import httpx
import pytest
import voluptuous as vol
from homeassistant.components import ai_task, conversation
from homeassistant.components.conversation import trace as conversation_trace
from homeassistant.components.conversation.chat_log import async_subscribe_chat_logs
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_assist import DOMAIN


@pytest.fixture
async def search_entry(hass: HomeAssistant):
    assert await async_setup_component(hass, "homeassistant", {})
    hass.config.country = "US"
    entry = MockConfigEntry(
        domain=DOMAIN, title="Codex Assist", unique_id=DOMAIN,
        data={"access_token": "test-access", "refresh_token": "test-refresh", "model": "gpt-5.4"},
        options={"web_search": True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def backend(monkeypatch):
    planned = deque()
    requests = []
    expected_token = "test-access"

    def respond(request):
        nonlocal expected_token
        assert request.method == "POST"
        path, status, payload = planned.popleft()
        assert request.url.path == path
        if path == "/oauth/token":
            assert request.url.host == "auth.openai.com"
            requests.append((path, parse_qs(request.content.decode())))
            expected_token = payload["access_token"]
        else:
            assert request.url.host == "chatgpt.com"
            assert request.headers["authorization"] == f"Bearer {expected_token}"
            requests.append((path, json.loads(request.content)))
        if isinstance(payload, list):
            return httpx.Response(
                status, headers={"content-type": "text/event-stream"},
                content="".join(f"data: {json.dumps(event)}\n\n" for event in payload),
            )
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for module in ("conversation", "ai_task"):
            monkeypatch.setattr(
                f"custom_components.codex_assist.{module}.get_async_client", lambda hass: client
            )
        yield planned, requests
    assert not planned, "Not all intended backend exchanges were exercised"


def message(text):
    return {
        "type": "message", "role": "assistant", "phase": "final_answer",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def text_events(text):
    return [
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.output_item.done", "item": message(text)},
    ]


def call_event(call_id, name, arguments):
    return {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": call_id, "name": name,
        "arguments": json.dumps(arguments), "status": "completed",
    }}


def model_round(planned, events):
    planned.append(("/backend-api/codex/responses", 200, events))


def search_reply(planned):
    planned.append(("/backend-api/codex/alpha/search", 200, {
        "output": "Example Documentation (https://example.com/docs)\nA synthetic search result.",
        "results": [
            {"title": "Example Documentation", "url": "https://example.com/docs"},
            {"title": "Unsafe", "url": "javascript:alert(1)"},
        ],
    }))


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
async def test_search_delivers_only_final_text_through_real_ha(
    hass: HomeAssistant, search_entry, backend, surface,
):
    planned, requests = backend
    model_round(planned, [
        *text_events("An unverified preliminary answer. "),
        call_event("search-1", "web_search", {"queries": ["synthetic documentation"]}),
    ])
    search_reply(planned)
    model_round(planned, [
        *text_events("I will open the result. "),
        call_event("search-2", "web_search", {
            "queries": [], "open_urls": ["https://example.com/docs"],
        }),
    ])
    search_reply(planned)
    model_round(planned, text_events("The verified final answer."))

    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Look up the documentation.", None, Context(),
            agent_id="conversation.codex_assist",
        )
        assert result.response.speech["plain"]["speech"] == "The verified final answer."
        assert result.response.card["simple"] == {
            "title": "Sources",
            "content": "- Example Documentation — <https://example.com/docs>",
        }
        model_round(planned, text_events("A follow-up answer."))
        followup = await conversation.async_converse(
            hass, "Summarize that.", result.conversation_id, Context(),
            agent_id="conversation.codex_assist",
        )
        assert followup.response.speech["plain"]["speech"] == "A follow-up answer."
        replay = requests[-1][1]["input"]
        assert replay == [
            *requests[-2][1]["input"],
            message("The verified final answer."),
            {"role": "user", "content": "Summarize that."},
        ]
        assert [item["call_id"] for item in replay if item.get("type") == "function_call"] == [
            "search-1", "search-2",
        ]
        assert [
            item["call_id"] for item in replay if item.get("type") == "function_call_output"
        ] == ["search-1", "search-2"]
    else:
        result = await ai_task.async_generate_data(
            hass, task_name="Search task", instructions="Look up the documentation.",
            entity_id="ai_task.codex_assist_ai_task",
        )
        assert isinstance(result, ai_task.GenDataTaskResult)
        assert result.data == "The verified final answer."

    searches = [body for path, body in requests if path.endswith("alpha/search")]
    assert len(searches) == 2
    assert searches[0]["settings"]["user_location"] == {"type": "approximate", "country": "US"}
    assert searches[1]["commands"] == {"open": [{"ref_id": "https://example.com/docs"}]}
    first_model = requests[0][1]
    assert not any(tool["type"] == "web_search" for tool in first_model["tools"])
    assert any(tool.get("name") == "web_search" for tool in first_model["tools"])


@pytest.mark.parametrize("replay_boundary", ["ha_handoff", "next_user"])
async def test_search_then_ha_tool_preserves_native_tool_pair_and_sources(
    hass: HomeAssistant, search_entry, backend, monkeypatch, replay_boundary, caplog, request,
):
    """Real HA executes a registered harmless tool, not a mocked chat-log stream."""
    executed = []
    conversation_trace.async_clear_traces()
    caplog.set_level(logging.DEBUG, logger=conversation.ChatLog.__module__)
    public_events = []
    request.addfinalizer(async_subscribe_chat_logs(
        hass, lambda conversation_id, event_type, data: public_events.append(data),
    ))

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
        return await ReadingAPI(
            hass=hass, id="search_test", name="Search test"
        ).async_get_api_instance(llm_context)

    monkeypatch.setattr(llm, "async_get_api", get_api)
    planned, requests = backend
    search_call = call_event("search-1", "web_search", {"queries": ["synthetic"]})
    search_reasoning = {"type": "reasoning", "encrypted_content": "private-search-reasoning"}
    hidden_text = "An unverified search preamble."
    model_round(planned, [
        {"type": "response.output_item.done", "item": search_reasoning},
        *text_events(hidden_text), search_call,
    ])
    fact = "Synthetic calibration constant: violet-otter-193."
    url = "https://example.com/calibration/retained-evidence"
    output = f"Calibration ({url})\n{fact}"
    planned.append(("/backend-api/codex/alpha/search", 200, {
        "output": output, "results": [{"title": "Calibration", "url": url}],
    }))
    ha_call = call_event("reading-1", "HassTestReading", {})
    skipped_call = call_event("skipped-search", "web_search", {"queries": ["unneeded"]})
    mixed_reasoning = {"type": "reasoning", "encrypted_content": "private-mixed-reasoning"}
    model_round(planned, [
        {"type": "response.output_item.done", "item": mixed_reasoning}, skipped_call, ha_call,
    ])
    # Neither the fact nor the URL is repeated by the handoff or final message.
    final_text = "The reading is seven."
    model_round(planned, text_events(final_text))
    result = await conversation.async_converse(
        hass, "Search and get the test reading.", None, Context(),
        agent_id="conversation.codex_assist",
    )
    assert result.response.speech["plain"]["speech"] == final_text
    assert url in result.response.card["simple"]["content"]
    handoff = requests[-1][1]["input"]
    model_round(planned, text_events("A follow-up answer."))
    followup = await conversation.async_converse(
        hass, "Compare that with the calibration.", result.conversation_id, Context(),
        agent_id="conversation.codex_assist",
    )
    assert followup.response.speech["plain"]["speech"] == "A follow-up answer."
    assert executed == ["reading-1"]
    assert len(requests) == 5
    replay = handoff if replay_boundary == "ha_handoff" else requests[-1][1]["input"]
    searched_output = {"type": "function_call_output", "call_id": "search-1", "output": output}
    assert searched_output in replay, "Searched fact and URL must survive the owner replay boundary"
    assert replay[:7] == [
        {"role": "user", "content": "Search and get the test reading."},
        search_reasoning, message(hidden_text), search_call["item"], searched_output,
        mixed_reasoning, skipped_call["item"],
    ]
    assert replay[7] == ha_call["item"]
    skipped_output = replay[8]
    assert skipped_output["type"] == "function_call_output"
    assert skipped_output["call_id"] == "skipped-search"
    assert "skipped" in skipped_output["output"].lower()
    assert replay[9] == {
        "type": "function_call_output", "call_id": "reading-1", "output": '{"reading":7}',
    }
    assert replay[10:] == ([] if replay_boundary == "ha_handoff" else [
        message(final_text), {"role": "user", "content": "Compare that with the calibration."},
    ])
    calls = [item["call_id"] for item in replay if item.get("type") == "function_call"]
    outputs = [item["call_id"] for item in replay if item.get("type") == "function_call_output"]
    assert calls == outputs == ["search-1", "skipped-search", "reading-1"]
    assert fact not in final_text and url not in final_text
    assert hidden_text not in result.response.speech["plain"]["speech"]
    assert public_events
    traces = str([trace.as_dict() for trace in conversation_trace.async_get_traces()])
    assert "'redacted': True" in traces
    native_count = len(handoff) - 2  # User and HA result are outside native state.
    assert f"'item_count': {native_count}" in traces
    assert f"CodexNativeState(item_count={native_count})" in caplog.text
    for private_text in (fact, hidden_text, "private-search-reasoning", "private-mixed-reasoning"):
        assert private_text not in caplog.text
        assert private_text not in traces
        assert private_text not in str(public_events)


@pytest.mark.parametrize("surface", ["conversation", "ai_task"])
async def test_search_401_refreshes_once_through_real_owner(
    hass: HomeAssistant, search_entry, backend, surface,
):
    """A rejected search refreshes through the existing coordinator, without leaked text."""
    planned, requests = backend
    first = [
        *text_events("An interrupted preliminary answer. "),
        call_event("search-rejected", "web_search", {"queries": ["synthetic"]}),
    ]
    model_round(planned, first)
    planned.append(("/backend-api/codex/alpha/search", 401, {"detail": "expired access token"}))
    planned.append(("/oauth/token", 200, {
        "access_token": "test-new-access", "refresh_token": "test-new-refresh",
    }))
    model_round(planned, [call_event("search-retry", "web_search", {"queries": ["synthetic"]})])
    search_reply(planned)
    model_round(planned, text_events("Recovered after refresh."))
    if surface == "conversation":
        result = await conversation.async_converse(
            hass, "Look up the documentation.", None, Context(),
            agent_id="conversation.codex_assist",
        )
        assert result.response.speech["plain"]["speech"] == "Recovered after refresh."
    else:
        result = await ai_task.async_generate_data(
            hass, task_name="Search task", instructions="Look up the documentation.",
            entity_id="ai_task.codex_assist_ai_task",
        )
        assert result.data == "Recovered after refresh."
    assert search_entry.data["access_token"] == "test-new-access"
    assert search_entry.data["refresh_token"] == "test-new-refresh"
    assert [path for path, _body in requests].count("/oauth/token") == 1
    assert requests[2][1]["grant_type"] == ["refresh_token"]
    assert requests[2][1]["refresh_token"] == ["test-refresh"]
    assert "search-rejected" not in json.dumps(requests[3][1]["input"])
