from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from tests.ha_fakes import install_homeassistant_fakes


def _flat_fields(schema):
    return {key.key: validator for key, validator in schema.schema.items()}


def _section_fields(schema, section_name):
    section_value = _flat_fields(schema)[section_name]
    return _flat_fields(section_value.schema)


def test_options_schema_keeps_saved_model_only_when_available(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.reload(
        importlib.import_module("custom_components.codex_assist.config_flow")
    )

    schema = module._settings_schema(
        {
            "model": "retired-model",
            "image_model": "bad-image-model",
            "image_size": "2048x2048",
        },
        model_options=["gpt-5.3-codex"],
    )

    chat_section = _flat_fields(schema)[module.SECTION_CHAT_SETTINGS]
    image_section = _flat_fields(schema)[module.SECTION_IMAGE_SETTINGS]
    chat_defaults = {key.key: key.default for key in chat_section.schema.schema}
    image_defaults = {key.key: key.default for key in image_section.schema.schema}
    assert chat_defaults["model"] == module.DEFAULT_MODEL
    assert image_defaults["image_model"] == "gpt-image-2-medium"
    assert image_defaults["image_size"] == "1024x1024"


def test_config_flow_returns_options_flow_instance(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.reload(
        importlib.import_module("custom_components.codex_assist.config_flow")
    )

    options_flow = module.CodexAssistConfigFlow.async_get_options_flow(object())

    assert isinstance(options_flow, module.CodexAssistOptionsFlow)


def test_first_run_schema_only_asks_for_chat_model(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.reload(
        importlib.import_module("custom_components.codex_assist.config_flow")
    )

    fields = _flat_fields(module._user_schema())

    assert list(fields) == ["model"]


def test_options_schema_groups_everyday_advanced_and_image_controls(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.reload(
        importlib.import_module("custom_components.codex_assist.config_flow")
    )

    schema = module._settings_schema({}, model_options=["gpt-5.4"])
    sections = _flat_fields(schema)

    assert list(sections) == [
        module.SECTION_CHAT_SETTINGS,
        module.SECTION_ADVANCED_SETTINGS,
        module.SECTION_IMAGE_SETTINGS,
    ]
    assert sections[module.SECTION_CHAT_SETTINGS].options == {"collapsed": False}
    assert sections[module.SECTION_ADVANCED_SETTINGS].options == {"collapsed": True}
    assert sections[module.SECTION_IMAGE_SETTINGS].options == {"collapsed": True}

    chat = _section_fields(schema, module.SECTION_CHAT_SETTINGS)
    advanced = _section_fields(schema, module.SECTION_ADVANCED_SETTINGS)
    images = _section_fields(schema, module.SECTION_IMAGE_SETTINGS)

    assert list(chat) == ["model", "text_verbosity", "web_search"]
    assert [option.value for option in chat["model"].config.options] == ["gpt-5.4"]
    assert chat["text_verbosity"].config.options == ["low", "medium", "high"]

    assert list(advanced) == ["prompt", "reasoning_effort"]
    assert advanced["reasoning_effort"].config.options == ["low", "medium", "high"]

    assert list(images) == ["image_model", "image_size"]
    assert [option.value for option in images["image_model"].config.options] == [
        "gpt-image-2-low",
        "gpt-image-2-medium",
        "gpt-image-2-high",
    ]
    assert [option.value for option in images["image_size"].config.options] == [
        "1024x1024",
        "1536x1024",
        "1024x1536",
    ]

    all_fields = {*chat, *advanced, *images}
    assert "reasoning_summary" not in all_fields
    assert "safety_mode" not in all_fields


def test_section_input_is_flattened_for_existing_runtime_settings(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.reload(
        importlib.import_module("custom_components.codex_assist.config_flow")
    )

    flattened = module._flatten_settings_input(
        {
            module.SECTION_CHAT_SETTINGS: {
                "model": "gpt-5.4",
                "text_verbosity": "low",
                "web_search": True,
            },
            module.SECTION_ADVANCED_SETTINGS: {
                "prompt": "Be brief.",
                "reasoning_effort": "medium",
            },
            module.SECTION_IMAGE_SETTINGS: {
                "image_model": "gpt-image-2-high",
                "image_size": "1536x1024",
            },
        }
    )

    assert flattened == {
        "model": "gpt-5.4",
        "text_verbosity": "low",
        "web_search": True,
        "prompt": "Be brief.",
        "reasoning_effort": "medium",
        "image_model": "gpt-image-2-high",
        "image_size": "1536x1024",
    }


@pytest.mark.asyncio
async def test_options_submission_preserves_hidden_reasoning_summary(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.reload(
        importlib.import_module("custom_components.codex_assist.config_flow")
    )
    flow = module.CodexAssistOptionsFlow()
    flow.config_entry = SimpleNamespace(
        data={"reasoning_summary": "detailed"},
        options={},
    )

    result = await flow.async_step_init(
        {
            module.SECTION_CHAT_SETTINGS: {
                "model": "gpt-5.4",
                "text_verbosity": "medium",
                "web_search": False,
            },
            module.SECTION_ADVANCED_SETTINGS: {
                "prompt": "Be brief.",
                "reasoning_effort": "low",
            },
            module.SECTION_IMAGE_SETTINGS: {
                "image_model": "gpt-image-2-medium",
                "image_size": "1024x1024",
            },
        }
    )

    assert result["type"] == "create_entry"
    assert result["data"]["reasoning_summary"] == "detailed"
