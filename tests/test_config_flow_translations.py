import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.codex_assist.codex_models import ModelCatalog
from tests.ha_fakes import install_homeassistant_fakes

TRANSLATION = Path("custom_components/codex_assist/translations/en.json")


def test_config_flow_device_step_has_visible_translation_description():
    assert TRANSLATION.exists()

    data = json.loads(TRANSLATION.read_text())
    device_step = data["config"]["step"]["device"]

    assert device_step["title"] == "Sign in with Codex"
    assert "{verification_uri}" in device_step["description"]
    assert "{user_code}" in device_step["description"]
    assert "{interval}" in device_step["description"]


def test_config_flow_errors_are_translated_for_device_polling():
    data = json.loads(TRANSLATION.read_text())
    errors = data["config"]["error"]

    assert "authorization_pending" in errors
    assert "device_code_auth_failed" in errors
    assert "device_code_request_failed" in errors


def test_strings_and_english_translation_are_identical():
    assert json.loads(Path("custom_components/codex_assist/strings.json").read_text()) == (
        json.loads(TRANSLATION.read_text())
    )


@pytest.mark.parametrize("subentry_type", ["conversation", "ai_task_data"])
async def test_every_rendered_subentry_field_and_outcome_is_translated(
    monkeypatch, subentry_type
):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.reload(importlib.import_module("custom_components.codex_assist.config_flow"))

    async def catalog(*args, **kwargs):
        return ModelCatalog(("account-model",), "discovered")

    monkeypatch.setattr(module, "async_entry_model_catalog", catalog)
    flow = module.CodexAssistSubentryFlow()
    flow.hass = object()
    flow.entry = SimpleNamespace(state="loaded", subentries={})
    flow._subentry_type = subentry_type
    flow.source = "user"
    form = await flow.async_step_user()
    rejected = await flow.async_step_user({"name": "x", "chat_settings": {"model": "nope"}})
    flow.entry.state = "not_loaded"
    aborted = await flow.async_step_user()

    strings = json.loads(TRANSLATION.read_text())["config_subentries"][subentry_type]
    step = strings["step"][form["step_id"]]
    for key, value in form["data_schema"].schema.items():
        if hasattr(value, "options"):
            section_strings = step["sections"][key.schema]
            assert section_strings["name"]
            assert {field.schema for field in value.schema.schema} <= set(section_strings["data"])
        else:
            assert step["data"][key.schema]
    assert "{model_status}" in step["description"]
    assert set(rejected["errors"].values()) | {"no_models"} <= set(strings["error"])
    assert {aborted["reason"], "reconfigure_successful"} <= set(strings["abort"])
    assert set(strings["initiate_flow"]) == {"user", "reconfigure"}
    assert strings["entry_type"]
