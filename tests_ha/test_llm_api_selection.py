"""Each conversation agent offers Codex the Home Assistant LLM APIs it was given."""

from __future__ import annotations

import pytest
import voluptuous as vol
from codex_entries import codex_entry
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.setup import async_setup_component

from custom_components.codex_assist import DOMAIN
from custom_components.codex_assist.codex_client import (
    CodexClient,
    CodexTextDelta,
    CodexToolCall,
    CodexToolCallDelta,
)

CREDENTIALS = {"access_token": "test-access-token", "refresh_token": "test-refresh-token"}
DOCS_TOOL_NAMES = ("DocsLookup", "docs.lookup", "web_search")
# Home Assistant 2026.11 added admin-only LLM APIs.
ADMIN_APIS = hasattr(llm.API, "requires_admin")


def _tool_result(data):
    return llm.ToolResult(data=data, error=False) if hasattr(llm, "ToolResult") else data


class DocsTool(llm.Tool):
    description = "Look up a test document."
    integration = DOMAIN
    parameters = vol.Schema({})

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self._calls = calls

    async def async_call(self, hass, tool_input, llm_context):
        self._calls.append(self.name)
        return _tool_result({"document": "found"})


class DocsAPI(llm.API):
    def __init__(self, hass: HomeAssistant, calls: list[str], *, requires_admin: bool) -> None:
        super().__init__(
            hass=hass,
            id="admin_docs" if requires_admin else "test_docs",
            name="Test docs",
            **({"requires_admin": True} if requires_admin else {}),
        )
        self._calls = calls

    async def async_get_api_instance(self, llm_context):
        return llm.APIInstance(
            api=self,
            api_prompt="Test documents are available.",
            llm_context=llm_context,
            tools=[DocsTool(name, self._calls) for name in DOCS_TOOL_NAMES],
        )


async def _converse(hass, monkeypatch, conversation_settings, *, reply_with_tool=None):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = codex_entry(CREDENTIALS, conversation=conversation_settings)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls: list[str] = []
    for requires_admin in (False, True) if ADMIN_APIS else (False,):
        llm.async_register_api(hass, DocsAPI(hass, calls, requires_admin=requires_admin))
    requests = []

    async def stream_turn(self: CodexClient, **kwargs):
        requests.append(kwargs)
        if reply_with_tool and len(requests) == 1:
            (name,) = (name for name in _offered(kwargs) if _unprefixed(name) == reply_with_tool)
            yield CodexToolCallDelta(CodexToolCall(id="call-1", name=name, arguments={}))
        else:
            yield CodexTextDelta("Done.")

    monkeypatch.setattr(CodexClient, "stream_turn", stream_turn)
    result = await conversation.async_converse(
        hass, "Look it up.", None, Context(), agent_id="conversation.codex_assist"
    )
    await hass.async_block_till_done()
    return result, requests, calls


def _offered(request):
    return [tool["name"] for tool in request["tools"] if tool["type"] == "function"]


def _unprefixed(name):
    # Before 2026.11, Home Assistant prefixed every merged tool with its API name.
    return name.rpartition("__")[2]


async def test_agents_without_a_selection_keep_assist_only(hass, monkeypatch):
    result, requests, _calls = await _converse(hass, monkeypatch, {})

    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert not set(DOCS_TOOL_NAMES) & {_unprefixed(name) for name in _offered(requests[0])}


async def test_a_chat_only_agent_offers_no_home_assistant_tools(hass, monkeypatch):
    _result, requests, _calls = await _converse(hass, monkeypatch, {"llm_hass_api": []})

    assert requests[0]["tools"] == []


@pytest.mark.parametrize("web_search", [False, True], ids=["search-off", "search-on"])
async def test_selected_apis_reach_codex_and_run_through_home_assistant(
    hass, monkeypatch, web_search
):
    result, requests, calls = await _converse(
        hass,
        monkeypatch,
        {"llm_hass_api": ["assist", "test_docs"], "web_search": web_search},
        reply_with_tool="DocsLookup",
    )

    offered = _offered(requests[0])
    assert "DocsLookup" in {_unprefixed(name) for name in offered}
    assert not any(name.endswith("docs.lookup") for name in offered)
    if web_search:
        # Hosted search owns the bare name; an API's tool of that name must not shadow it.
        assert "web_search" not in offered
    else:
        assert "web_search" in {_unprefixed(name) for name in offered}
    hosted = [tool for tool in requests[0]["tools"] if tool["type"] == "web_search"]
    assert len(hosted) == web_search
    assert calls == ["DocsLookup"]
    assert result.response.speech["plain"]["speech"] == "Done."


@pytest.mark.skipif(not ADMIN_APIS, reason="admin-only LLM APIs start in Home Assistant 2026.11")
async def test_admin_only_apis_are_refused_without_an_admin_user(hass, monkeypatch):
    result, requests, calls = await _converse(
        hass, monkeypatch, {"llm_hass_api": ["assist", "admin_docs"]}
    )

    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert requests == []
    assert calls == []
