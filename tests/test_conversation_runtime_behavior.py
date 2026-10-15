from __future__ import annotations

import importlib

import pytest

from custom_components.codex_assist.codex_auth import CodexAuthTemporaryError, CodexTokenSet
from custom_components.codex_assist.codex_client import CodexRateLimitError
from tests.ha_fakes import install_homeassistant_fakes


class FakeAuthClient:
    async def refresh(self, tokens):
        assert tokens == CodexTokenSet("access-1", "refresh-1")
        return CodexTokenSet("access-2", "refresh-2")


class FakeConfigEntries:
    def __init__(self):
        self.updates = []

    def async_update_entry(self, entry, *, data):
        self.updates.append((entry, data))
        entry.data = data


class FakeEntry:
    def __init__(self):
        self.entry_id = "entry-1"
        self.data = {
            "model": "gpt-5.4",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
        }
        self.reauth_started = False

    def async_start_reauth(self, hass):
        self.reauth_started = True
        self.reauth_hass = hass


class FakeUserInput:
    language = "en"
    conversation_id = "conversation-1"
    agent_id = "conversation.codex_assist"
    extra_system_prompt = None

    def as_llm_context(self, domain):
        return {"domain": domain}


class FakeHandleMessageChatLog:
    def __init__(self):
        self.content = []
        self.added = []
        self.llm_api = None

    async def async_provide_llm_data(self, *args):
        self.provided_args = args

    def async_add_assistant_content_without_tools(self, content):
        self.added.append(content)


@pytest.fixture
def conversation_module(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.import_module("custom_components.codex_assist.conversation")
    return importlib.reload(module)


@pytest.mark.asyncio
async def test_refresh_runtime_tokens_persists_rotated_tokens(conversation_module):
    hass = type("Hass", (), {"config_entries": FakeConfigEntries()})()
    entry = FakeEntry()

    tokens = await conversation_module._refresh_runtime_tokens(
        hass,
        entry,
        FakeAuthClient(),
        CodexTokenSet("access-1", "refresh-1"),
    )

    assert tokens == CodexTokenSet("access-2", "refresh-2")
    assert hass.config_entries.updates == [
        (
            entry,
            {
                "model": "gpt-5.4",
                "access_token": "access-2",
                "refresh_token": "refresh-2",
            },
        )
    ]


def test_start_reauth_result_starts_ha_reauth_and_returns_user_message(
    conversation_module,
):
    hass = object()
    entry = FakeEntry()
    response = conversation_module.intent.IntentResponse(language="en")

    result = conversation_module._start_reauth_result(
        hass,
        entry,
        response,
        FakeUserInput(),
    )

    assert entry.reauth_started is True
    assert entry.reauth_hass is hass
    assert result.conversation_id == "conversation-1"
    assert response.speech == "Codex Assist needs attention in Home Assistant."


@pytest.mark.asyncio
async def test_handle_message_reports_temporary_auth_failure_without_reauth(
    conversation_module,
    monkeypatch,
):
    async def fail_temporarily(*args, **kwargs):
        raise CodexAuthTemporaryError("rate limited")

    hass = type(
        "Hass",
        (),
        {"http_client": None, "config_entries": FakeConfigEntries()},
    )()
    entry = FakeEntry()
    entry.options = {}
    entity = conversation_module.CodexAssistConversationEntity(entry)
    entity.hass = hass
    chat_log = FakeHandleMessageChatLog()

    class FailingCoordinator:
        async def resolve(self, *args, **kwargs):
            return await fail_temporarily()

    monkeypatch.setattr(
        conversation_module,
        "runtime_token_coordinator",
        lambda entry: FailingCoordinator(),
    )

    result = await entity._async_handle_message(FakeUserInput(), chat_log)

    assert entry.reauth_started is False
    assert result[1] is chat_log
    assert len(chat_log.added) == 1
    assert chat_log.added[0].content == "Sorry, I couldn't complete that."


@pytest.mark.asyncio
async def test_handle_message_reports_friendly_usage_limit_without_status_code(
    conversation_module,
    monkeypatch,
):
    async def resolve_tokens(*args, **kwargs):
        return CodexTokenSet("access-1", "refresh-1")

    async def raise_rate_limit(*args, **kwargs):
        raise CodexRateLimitError("Codex usage limit or rate limit reached: quota exceeded")

    hass = type(
        "Hass",
        (),
        {"http_client": None, "config_entries": FakeConfigEntries()},
    )()
    entry = FakeEntry()
    entry.options = {}
    entity = conversation_module.CodexAssistConversationEntity(entry)
    entity.hass = hass
    entity.entity_id = "conversation.codex_assist"
    chat_log = FakeHandleMessageChatLog()

    class ResolvedCoordinator:
        async def resolve(self, *args, **kwargs):
            return await resolve_tokens()

    monkeypatch.setattr(
        conversation_module,
        "runtime_token_coordinator",
        lambda entry: ResolvedCoordinator(),
    )
    monkeypatch.setattr(
        conversation_module,
        "_stream_codex_turn_into_chat_log",
        raise_rate_limit,
    )

    result = await entity._async_handle_message(FakeUserInput(), chat_log)

    assert result[1] is chat_log
    assert len(chat_log.added) == 1
    assert chat_log.added[0].content == (
        "Codex Assist cannot process this request because of an account or usage limit."
    )
    assert "status 429" not in chat_log.added[0].content
    assert "Codex request failed" not in chat_log.added[0].content
