from __future__ import annotations

import importlib
import types

import pytest

from custom_components.codex_assist.codex_auth import (
    CodexAuthorizationCode,
    CodexDeviceCode,
    CodexTokenSet,
)
from tests.ha_fakes import install_homeassistant_fakes


class FakeAuthClient:
    def __init__(self):
        self.device_code = CodexDeviceCode(
            user_code="ABCD-EFGH",
            device_auth_id="device-1",
            verification_uri="https://auth.openai.com/codex/device",
            interval=7,
        )
        self.poll_result = CodexAuthorizationCode("auth-code-1", "verifier-1")
        self.tokens = CodexTokenSet("access-1", "refresh-1")
        self.requested = 0
        self.polls = []
        self.exchanges = []

    async def request_device_code(self):
        self.requested += 1
        return self.device_code

    async def poll_device_code(self, *, device_auth_id, user_code):
        self.polls.append((device_auth_id, user_code))
        return self.poll_result

    async def exchange_authorization_code(self, authorization):
        self.exchanges.append(authorization)
        return self.tokens


@pytest.fixture
def config_flow_module(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.import_module("custom_components.codex_assist.config_flow")
    return importlib.reload(module)


@pytest.mark.asyncio
async def test_config_flow_requests_device_code_before_showing_pairing_form(
    config_flow_module,
):
    flow = config_flow_module.CodexAssistConfigFlow()
    auth = FakeAuthClient()
    flow._auth_client = lambda: auth

    result = await flow.async_step_user({"model": "gpt-5.4"})

    assert auth.requested == 1
    assert result["type"] == "form"
    assert result["step_id"] == "device"
    assert result["description_placeholders"] == {
        "verification_uri": "https://auth.openai.com/codex/device",
        "user_code": "ABCD-EFGH",
        "interval": "7",
    }
    assert not hasattr(flow, "unique_id")
    assert not hasattr(flow, "duplicate_checked")


@pytest.mark.asyncio
async def test_config_flow_creates_entry_only_after_device_token_exchange(
    config_flow_module,
):
    flow = config_flow_module.CodexAssistConfigFlow()
    auth = FakeAuthClient()
    flow._auth_client = lambda: auth
    flow._setup_input = {"model": "gpt-5.4"}
    flow._device_code = auth.device_code
    flow.source = "user"

    result = await flow.async_step_device_wait()

    assert auth.polls == [("device-1", "ABCD-EFGH")]
    assert auth.exchanges == [CodexAuthorizationCode("auth-code-1", "verifier-1")]
    assert flow.unique_id == "codex_assist"
    assert flow.duplicate_checked is True
    assert result == {
        "type": "create_entry",
        "title": "Codex Assist",
        "data": {
            "model": "gpt-5.4",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
        },
    }


@pytest.mark.asyncio
async def test_config_flow_pending_authorization_keeps_same_device_code(
    config_flow_module,
):
    flow = config_flow_module.CodexAssistConfigFlow()
    auth = FakeAuthClient()
    auth.poll_result = None
    flow._auth_client = lambda: auth
    flow._device_code = auth.device_code

    result = await flow.async_step_device_wait()

    assert result["type"] == "form"
    assert result["step_id"] == "device"
    assert result["errors"] == {"base": "authorization_pending"}
    assert result["description_placeholders"]["user_code"] == "ABCD-EFGH"


@pytest.mark.asyncio
async def test_reauth_updates_existing_entry_after_device_token_exchange(
    config_flow_module,
):
    flow = config_flow_module.CodexAssistConfigFlow()
    auth = FakeAuthClient()
    flow._auth_client = lambda: auth
    flow._setup_input = {"model": "gpt-5.4"}
    flow._device_code = auth.device_code
    flow.source = "reauth"
    flow.reauth_entry = object()

    result = await flow.async_step_device_wait()

    assert result["type"] == "abort"
    assert result["data_updates"] == {
        "model": "gpt-5.4",
        "access_token": "access-1",
        "refresh_token": "refresh-1",
    }


def _schema_defaults(data_schema) -> dict[str, object]:
    return {
        field.key: field.default
        for field in data_schema.schema
        if hasattr(field, "key") and hasattr(field, "default")
    }


def _section_defaults(data_schema, section_name) -> dict[str, object]:
    sections = {
        field.key: validator
        for field, validator in data_schema.schema.items()
    }
    return _schema_defaults(sections[section_name].schema)


def test_web_search_option_is_default_off_and_preserves_opt_in(config_flow_module):
    default_schema = config_flow_module._settings_schema({}, model_options=["gpt-5.4"])
    enabled_schema = config_flow_module._settings_schema(
        {"web_search": True}, model_options=["gpt-5.4"]
    )

    section_name = config_flow_module.SECTION_CHAT_SETTINGS
    assert _section_defaults(default_schema, section_name)["web_search"] is False
    assert _section_defaults(enabled_schema, section_name)["web_search"] is True


def _reconfigure_entry(**overrides):
    data = {
        "model": "gpt-5.4",
        "prompt": "Saved prompt.",
        "reasoning_effort": "high",
        "reasoning_summary": "detailed",
        "text_verbosity": "low",
        "web_search": True,
        "image_model": "gpt-image-2-high",
        "image_size": "1536x1024",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
    }
    options = {}
    data.update(overrides.pop("data", {}))
    options.update(overrides.pop("options", {}))
    return types.SimpleNamespace(data=data, options=options, **overrides)


@pytest.mark.asyncio
async def test_reconfigure_only_asks_user_to_sign_in_again(config_flow_module):
    flow = config_flow_module.CodexAssistConfigFlow()
    flow.source = "reconfigure"
    flow.reconfigure_entry = _reconfigure_entry()

    result = await flow.async_step_reconfigure()

    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert _schema_defaults(result["data_schema"]) == {}


@pytest.mark.asyncio
async def test_reconfigure_updates_existing_entry_after_device_token_exchange(
    config_flow_module,
):
    flow = config_flow_module.CodexAssistConfigFlow()
    auth = FakeAuthClient()
    flow._auth_client = lambda: auth
    flow._setup_input = {}
    flow._device_code = auth.device_code
    flow.source = "reconfigure"
    flow.reconfigure_entry = _reconfigure_entry()

    result = await flow.async_step_device_wait()

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert result["entry"] is flow.reconfigure_entry
    assert result["data_updates"] == {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
    }


@pytest.mark.asyncio
async def test_reconfigure_device_code_request_failure_shows_error(config_flow_module):
    flow = config_flow_module.CodexAssistConfigFlow()
    auth = FakeAuthClient()

    async def request_device_code():
        raise RuntimeError("network error")

    auth.request_device_code = request_device_code
    flow._auth_client = lambda: auth
    flow.source = "reconfigure"
    flow.reconfigure_entry = _reconfigure_entry()

    result = await flow.async_step_reconfigure({})

    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "device_code_request_failed"}
    assert _schema_defaults(result["data_schema"]) == {}
