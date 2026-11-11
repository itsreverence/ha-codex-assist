from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from custom_components.codex_assist.codex_models import ModelCatalog
from tests.ha_fakes import install_homeassistant_fakes

PROMPT = "# House rules\n\n- **Never** unlock doors.\n- Reply concisely."


@pytest.fixture
def module(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.reload(importlib.import_module("custom_components.codex_assist.config_flow"))
    _use_catalog(monkeypatch, module, ("account-model", "second-model"), "discovered")
    return module


def _use_catalog(monkeypatch, module, *catalog):
    async def entry_model_catalog(hass, entry, *, force=False):
        assert force
        return ModelCatalog(*catalog)

    monkeypatch.setattr(module, "async_entry_model_catalog", entry_model_catalog)


def _flow(module, subentry_type, *, subentry=None, state="loaded", titles=()):
    flow = module.CodexAssistSubentryFlow()
    flow.hass = object()
    flow.entry = SimpleNamespace(
        state=state,
        subentries={title: SimpleNamespace(title=title) for title in titles},
    )
    flow._subentry_type = subentry_type
    flow.source = "user" if subentry is None else "reconfigure"
    flow.subentry = subentry
    return flow


def _fields(schema):
    return {key.schema: validator for key, validator in schema.schema.items()}


def _form_layout(result):
    fields = _fields(result["data_schema"])
    return {
        key: list(_fields(value.schema)) if hasattr(value, "options") else value
        for key, value in fields.items()
    }


def _section_field(result, section_name, key):
    return _fields(_fields(result["data_schema"])[section_name].schema)[key]


def _section_defaults(result, section_name):
    section_value = _fields(result["data_schema"])[section_name]
    return {key.schema: key.default for key in section_value.schema.schema}


def _chat(**values):
    return {"chat_settings": {"text_verbosity": "medium", "web_search": False, **values}}


async def test_adding_a_conversation_agent_asks_for_name_prompt_and_chat_settings(module):
    flow = _flow(module, "conversation")

    result = await flow.async_step_user()

    assert result["step_id"] == "init"
    assert _form_layout(result) == {
        "name": str,
        "chat_settings": ["model", "text_verbosity", "web_search"],
        "advanced_settings": ["prompt", "reasoning_effort"],
    }
    assert next(iter(result["data_schema"].schema)).default == "Codex Assist"
    assert _section_defaults(result, "chat_settings")["model"] == "account-model"
    assert _section_defaults(result, "chat_settings")["web_search"] is False
    prompt_selector = _section_field(result, "advanced_settings", "prompt")
    assert isinstance(prompt_selector, module.selector.TextSelector)
    assert prompt_selector.config.multiline is True

    created = await flow.async_step_user(
        {
            "name": "Kitchen agent",
            **_chat(model="second-model", web_search=True),
            "advanced_settings": {"prompt": PROMPT, "reasoning_effort": "high"},
        }
    )

    assert created == {
        "type": "create_entry",
        "title": "Kitchen agent",
        "data": {
            "model": "second-model",
            "text_verbosity": "medium",
            "web_search": True,
            "prompt": PROMPT,
            "reasoning_effort": "high",
        },
    }


async def test_ai_task_settings_have_image_defaults_but_no_prompt(module):
    flow = _flow(module, "ai_task_data")

    result = await flow.async_step_user()

    assert _form_layout(result) == {
        "name": str,
        "chat_settings": ["model", "text_verbosity", "web_search"],
        "advanced_settings": ["reasoning_effort"],
        "image_settings": ["image_model", "image_size"],
    }
    assert next(iter(result["data_schema"].schema)).default == "Codex Assist AI Task"
    image_model = _section_field(result, "image_settings", "image_model")
    image_size = _section_field(result, "image_settings", "image_size")
    assert [o.value for o in image_model.config.options] == [
        "gpt-image-2-low",
        "gpt-image-2-medium",
        "gpt-image-2-high",
    ]
    assert [o.value for o in image_size.config.options] == ["1024x1024", "1536x1024", "1024x1536"]

    created = await flow.async_step_user(
        {
            "name": "Second AI Task",
            **_chat(model="account-model"),
            "advanced_settings": {"reasoning_effort": "low", "prompt": "ignored"},
            "image_settings": {"image_model": "gpt-image-2-high", "image_size": "1536x1024"},
        }
    )

    assert created["data"] == {
        "model": "account-model",
        "text_verbosity": "medium",
        "web_search": False,
        "reasoning_effort": "low",
        "image_model": "gpt-image-2-high",
        "image_size": "1536x1024",
    }


async def test_adding_an_agent_rejects_a_model_the_account_does_not_list(module):
    flow = _flow(module, "conversation")

    result = await flow.async_step_user({"name": "Agent", **_chat(model="guessed-model")})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_model"}


async def test_adding_an_agent_without_any_listed_model_is_refused(module, monkeypatch):
    _use_catalog(monkeypatch, module, (), "discovered")
    flow = _flow(module, "conversation")

    result = await flow.async_step_user()
    assert "model" not in _section_defaults(result, "chat_settings")
    assert "no visible models" in result["description_placeholders"]["model_status"]

    result = await flow.async_step_user({"name": "Agent", **_chat()})
    assert result["errors"] == {"base": "no_models"}


async def test_reconfigure_keeps_an_unlisted_saved_model_and_hidden_settings(
    module, monkeypatch
):
    _use_catalog(monkeypatch, module, ("account-model",), "cached", "unavailable")
    subentry = SimpleNamespace(
        data={
            "model": "retired-model",
            "prompt": "Saved prompt.",
            "reasoning_effort": "high",
            "reasoning_summary": "detailed",
            "text_verbosity": "low",
            "web_search": True,
        }
    )
    flow = _flow(module, "conversation", subentry=subentry)

    result = await flow.async_step_reconfigure()

    assert "name" not in _form_layout(result)
    assert _section_defaults(result, "chat_settings") == {
        "model": "retired-model",
        "text_verbosity": "low",
        "web_search": True,
    }
    choices = _section_field(result, "chat_settings", "model").config.options
    assert [(c.value, c.label) for c in choices] == [
        ("account-model", "account-model"),
        ("retired-model", "retired-model (saved; not currently listed)"),
    ]
    status = result["description_placeholders"]["model_status"]
    assert "cached list may be outdated" in status
    assert "saved model" in status

    rejected = await flow.async_step_reconfigure(_chat(model="guessed-model"))
    assert rejected["errors"] == {"base": "invalid_model"}

    saved = await flow.async_step_reconfigure(_chat(model="retired-model", text_verbosity="high"))
    assert saved == {"type": "abort", "reason": "reconfigure_successful"}
    assert subentry.data == {
        "model": "retired-model",
        "prompt": "Saved prompt.",
        "reasoning_effort": "high",
        "reasoning_summary": "detailed",
        "text_verbosity": "high",
        "web_search": False,
    }


async def test_reconfigure_with_an_empty_account_list_offers_only_the_saved_model(
    module, monkeypatch
):
    _use_catalog(monkeypatch, module, (), "discovered")
    flow = _flow(module, "ai_task_data", subentry=SimpleNamespace(data={"model": "saved-model"}))

    result = await flow.async_step_reconfigure()

    choices = _section_field(result, "chat_settings", "model").config.options
    assert [c.value for c in choices] == ["saved-model"]


@pytest.mark.parametrize("source", ["user", "reconfigure"])
async def test_agent_settings_need_the_sign_in_to_be_loaded(module, source):
    subentry = None if source == "user" else SimpleNamespace(data={})
    flow = _flow(module, "conversation", subentry=subentry, state="not_loaded")

    step = flow.async_step_user if source == "user" else flow.async_step_reconfigure
    result = await step()

    assert result == {"type": "abort", "reason": "entry_not_loaded"}


def test_both_agent_kinds_are_offered_as_subentries(module):
    types = module.CodexAssistConfigFlow.async_get_supported_subentry_types(object())

    assert types == {
        "conversation": module.CodexAssistSubentryFlow,
        "ai_task_data": module.CodexAssistSubentryFlow,
    }


def test_first_run_signs_in_before_choosing_a_model(module):
    assert list(_fields(module._user_schema())) == []


@pytest.mark.parametrize(
    "models, expected",
    [
        (["new-model", "gpt-5.6-terra"], "new-model"),
        (["new-model"], "new-model"),
    ],
)
def test_setup_default_is_always_one_of_the_discovered_models(module, models, expected):
    schema = module._model_schema({}, model_options=models)
    assert next(iter(schema.schema)).default == expected
    assert [o.value for o in _fields(schema)["model"].config.options] == models


def test_empty_account_list_does_not_get_populated_with_defaults(module):
    assert not module._model_schema({}, model_options=[]).schema


async def test_adding_an_agent_suggests_a_name_no_agent_uses_yet(module):
    flow = _flow(module, "conversation", titles=("Codex Assist", "Codex Assist 2", "Pirate"))

    result = await flow.async_step_user()

    assert next(iter(result["data_schema"].schema)).default == "Codex Assist 3"
