from __future__ import annotations

import importlib

import pytest

from custom_components.codex_assist.codex_auth import (
    CodexAuthTemporaryError,
    CodexReauthRequiredError,
    CodexTokenSet,
)
from custom_components.codex_assist.codex_client import CodexAuthenticationError
from tests.ha_fakes import install_homeassistant_fakes


@pytest.fixture
def ai_task_module(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    importlib.reload(importlib.import_module("custom_components.codex_assist.conversation"))
    module = importlib.import_module("custom_components.codex_assist.ai_task")
    return importlib.reload(module)


def test_ai_task_entity_advertises_data_image_and_attachment_support(ai_task_module):
    entity = ai_task_module.CodexAssistAITaskEntity(type("Entry", (), {"entry_id": "abc"})())

    assert entity._attr_name == "Codex Assist AI Task"
    assert entity._attr_unique_id == "abc_ai_task"
    assert entity._attr_supported_features == 7


def test_structured_data_from_text_returns_plain_text_without_structure(ai_task_module):
    assert ai_task_module._structured_data_from_text("plain response", None) == "plain response"


def test_structured_data_from_text_parses_json_when_structure_requested(ai_task_module):
    structure = ai_task_module.vol.Schema({"state": str})

    assert ai_task_module._structured_data_from_text('{"state":"on"}', structure) == {"state": "on"}


def test_structured_data_from_text_rejects_invalid_json_when_structure_requested(
    ai_task_module,
    caplog,
):
    with pytest.raises(RuntimeError, match="invalid JSON"):
        ai_task_module._structured_data_from_text(
            "not json", ai_task_module.vol.Schema({"state": str})
        )
    assert "not json" not in caplog.text
    assert "Failed to parse Codex Assist AI Task JSON response (8 chars)" in caplog.text


def test_structured_data_from_text_validates_requested_structure(ai_task_module):
    class RejectingStructure:
        def __call__(self, value):
            raise ai_task_module.vol.Invalid("state is required")

    with pytest.raises(RuntimeError, match="did not match the requested structure"):
        ai_task_module._structured_data_from_text('{"wrong":"shape"}', RejectingStructure())


def test_structured_output_format_converts_ai_task_schema(ai_task_module):
    task = type(
        "Task",
        (),
        {
            "name": "Porch State",
            "structure": ai_task_module.vol.Schema({"state": str}),
        },
    )()
    chat_log = type(
        "ChatLog",
        (),
        {"llm_api": type("LLMApi", (), {"custom_serializer": None})()},
    )()

    assert ai_task_module._structured_output_format(task, chat_log) == {
        "type": "json_schema",
        "name": "porch_state",
        "schema": {"state": str},
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("x" * 65, "x" * 64),
        ("!!!", "structured_output"),
    ],
)
def test_structured_output_format_normalizes_api_name(ai_task_module, name, expected):
    task = type(
        "Task",
        (),
        {"name": name, "structure": ai_task_module.vol.Schema({"state": str})},
    )()
    chat_log = type("ChatLog", (), {"llm_api": None})()

    assert ai_task_module._structured_output_format(task, chat_log)["name"] == expected


def test_ai_task_web_search_is_disabled_for_structured_output(ai_task_module):
    assert ai_task_module._web_search_enabled(True, text_format=None) is True
    assert ai_task_module._web_search_enabled(True, text_format={"type": "json_schema"}) is False
    assert ai_task_module._web_search_enabled(False, text_format=None) is False


@pytest.mark.asyncio
async def test_ai_task_generate_data_reports_temporary_auth_without_reauth(
    ai_task_module,
    monkeypatch,
):
    async def fail_temporarily(*args, **kwargs):
        raise CodexAuthTemporaryError("rate limited")

    class Entry:
        entry_id = "abc"
        data = {}
        options = {}
        reauth_started = False

        def async_start_reauth(self, hass):
            self.reauth_started = True

    entry = Entry()
    entity = ai_task_module.CodexAssistAITaskEntity(entry)
    entity.hass = type(
        "Hass",
        (),
        {"http_client": None, "config_entries": object()},
    )()
    task = type("Task", (), {"structure": None})()
    chat_log = type("ChatLog", (), {})()

    class FailingCoordinator:
        async def resolve(self, *args, **kwargs):
            return await fail_temporarily()

    monkeypatch.setattr(
        ai_task_module,
        "runtime_token_coordinator",
        lambda entry: FailingCoordinator(),
    )

    with pytest.raises(RuntimeError, match="rate limited"):
        await entity._async_generate_data(task, chat_log)

    assert entry.reauth_started is False


@pytest.mark.asyncio
async def test_ai_task_chat_log_retry_propagates_reauth_required(
    ai_task_module,
    monkeypatch,
):
    async def reject_access_token(**kwargs):
        raise CodexAuthenticationError("invalid token")

    class ReauthAuthClient:
        async def refresh(self, tokens):
            raise CodexReauthRequiredError("invalid refresh")

    chat_log = type(
        "ChatLog",
        (),
        {"unresponded_tool_results": False, "content": [], "llm_api": None},
    )()
    monkeypatch.setattr(
        ai_task_module,
        "_stream_codex_turn_into_chat_log",
        reject_access_token,
    )

    with pytest.raises(CodexReauthRequiredError):
        await ai_task_module._run_codex_ai_task_chat_log(
            hass=type(
                "Hass",
                (),
                {
                    "http_client": None,
                    "config_entries": type(
                        "ConfigEntries",
                        (),
                        {"async_update_entry": lambda self, entry, *, data: None},
                    )(),
                },
            )(),
            entry=type(
                "Entry",
                (),
                {"data": {"access_token": "access-1", "refresh_token": "refresh-1"}},
            )(),
            auth_client=ReauthAuthClient(),
            tokens=CodexTokenSet("access-1", "refresh-1"),
            codex=object(),
            chat_log=chat_log,
            entity_id="ai_task.codex_assist",
            model="gpt-5.4",
            prompt="Be concise.",
            reasoning_effort="low",
            reasoning_summary="auto",
            text_verbosity="medium",
        )


@pytest.mark.asyncio
async def test_ai_task_chat_log_retry_reauths_when_refreshed_token_is_rejected(
    ai_task_module,
    monkeypatch,
):
    calls = 0

    async def reject_both_tokens(**kwargs):
        nonlocal calls
        calls += 1
        raise CodexAuthenticationError("invalid token")

    class RefreshAuthClient:
        async def refresh(self, tokens):
            return CodexTokenSet("access-2", "refresh-2")

    chat_log = type(
        "ChatLog",
        (),
        {"unresponded_tool_results": False, "content": [], "llm_api": None},
    )()
    monkeypatch.setattr(
        ai_task_module,
        "_stream_codex_turn_into_chat_log",
        reject_both_tokens,
    )

    with pytest.raises(CodexReauthRequiredError, match="rejected after refresh"):
        await ai_task_module._run_codex_ai_task_chat_log(
            hass=type(
                "Hass",
                (),
                {
                    "http_client": None,
                    "config_entries": type(
                        "ConfigEntries",
                        (),
                        {"async_update_entry": lambda self, entry, *, data: None},
                    )(),
                },
            )(),
            entry=type(
                "Entry",
                (),
                {"data": {"access_token": "access-1", "refresh_token": "refresh-1"}},
            )(),
            auth_client=RefreshAuthClient(),
            tokens=CodexTokenSet("access-1", "refresh-1"),
            codex=object(),
            chat_log=chat_log,
            entity_id="ai_task.codex_assist",
            model="gpt-5.4",
            prompt="Be concise.",
            reasoning_effort="low",
            reasoning_summary="auto",
            text_verbosity="medium",
        )

    assert calls == 2


@pytest.mark.asyncio
async def test_ai_task_image_retry_reauths_when_refreshed_token_is_rejected(
    ai_task_module,
    monkeypatch,
):
    class RejectingImageCodex:
        async def generate_image(self, **kwargs):
            raise CodexAuthenticationError("invalid token")

    class RefreshAuthClient:
        async def refresh(self, tokens):
            return CodexTokenSet("access-2", "refresh-2")

    class FakeCodexClient(RejectingImageCodex):
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    chat_log = type("ChatLog", (), {"content": [], "llm_api": None})()
    task = type("Task", (), {"instructions": "draw it"})()
    monkeypatch.setattr(ai_task_module, "CodexClient", FakeCodexClient)

    with pytest.raises(CodexReauthRequiredError, match="rejected after refresh"):
        await ai_task_module._generate_codex_ai_task_image(
            hass=type(
                "Hass",
                (),
                {
                    "http_client": None,
                    "config_entries": type(
                        "ConfigEntries",
                        (),
                        {"async_update_entry": lambda self, entry, *, data: None},
                    )(),
                },
            )(),
            entry=type(
                "Entry",
                (),
                {"data": {"access_token": "access-1", "refresh_token": "refresh-1"}},
            )(),
            auth_client=RefreshAuthClient(),
            tokens=CodexTokenSet("access-1", "refresh-1"),
            codex=RejectingImageCodex(),
            chat_log=chat_log,
            task=task,
            chat_model="gpt-5.4",
            image_model="gpt-image-2-medium",
            image_size="1024x1024",
        )


@pytest.mark.asyncio
async def test_ai_task_uses_one_tools_disabled_turn_after_five_tool_rounds(
    ai_task_module,
    monkeypatch,
):
    calls = []

    async def stream_turn(**kwargs):
        calls.append(kwargs)
        chat_log.unresponded_tool_results = kwargs["allow_tools"]
        return kwargs["allow_tools"]

    monkeypatch.setattr(ai_task_module, "_stream_codex_turn_into_chat_log", stream_turn)

    tool = type(
        "Tool",
        (),
        {
            "name": "HassTurnOn",
            "description": "Turn on an exposed Home Assistant entity.",
            "parameters": ai_task_module.vol.Schema({}),
        },
    )()
    llm_api = type(
        "LLMApi",
        (),
        {"tools": [tool], "custom_serializer": None},
    )()
    chat_log = type(
        "ChatLog",
        (),
        {"unresponded_tool_results": True, "content": [], "llm_api": llm_api},
    )()
    await ai_task_module._run_codex_ai_task_chat_log(
        hass=object(),
        entry=object(),
        auth_client=object(),
        tokens=CodexTokenSet("access-1", "refresh-1"),
        codex=object(),
        chat_log=chat_log,
        entity_id="ai_task.codex_assist",
        model="gpt-5.4",
        prompt="Be concise.",
        reasoning_effort="low",
        reasoning_summary="auto",
        text_verbosity="medium",
    )

    assert [call["allow_tools"] for call in calls] == [True, True, True, True, True, False]
    assert [bool(call["tools"]) for call in calls] == [True, True, True, True, True, False]
    assert all(call["tools"][0]["name"] == "HassTurnOn" for call in calls[:5])
