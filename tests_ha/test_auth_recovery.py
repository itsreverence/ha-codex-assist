from __future__ import annotations

import base64
import json
import time
import traceback

import httpx
import pytest
from homeassistant.components import ai_task, conversation
from homeassistant.core import Context
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_assist import DOMAIN
from custom_components.codex_assist.codex_models import ModelDiscoveryCache
from custom_components.codex_assist.codex_runtime import runtime_token_coordinator
from custom_components.codex_assist.config_flow import SECTION_CHAT_SETTINGS
from custom_components.codex_assist.model_discovery import async_entry_model_catalog

USER_CODE_PATH = "/api/accounts/deviceauth/usercode"
POLL_PATH = "/api/accounts/deviceauth/token"
TOKEN_PATH = "/oauth/token"
MODEL_PATH = "/backend-api/codex/models"
RESPONSE_PATH = "/backend-api/codex/responses"


@pytest.fixture
async def auth_http(monkeypatch):
    queued = []
    requests = []

    def respond(request):
        requests.append(request)
        assert queued, f"Unexpected HTTP request to {request.url.path}"
        path, response = queued.pop(0)
        assert request.url.path == path
        if isinstance(response, Exception):
            raise response
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for module in ("config_flow", "model_discovery", "conversation", "ai_task"):
            monkeypatch.setattr(
                f"custom_components.codex_assist.{module}.get_async_client", lambda hass: client
            )
        yield queued, requests
    assert not queued


def _entry(access_token="test-access"):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Codex Assist",
        data={"access_token": access_token, "refresh_token": "test-refresh", "model": "gpt-5.4"},
    )


def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _device_response():
    return httpx.Response(200, json={"user_code": "TEST-CODE", "device_auth_id": "test-device"})


def _poll_response():
    return httpx.Response(
        200, json={"authorization_code": "test-code", "code_verifier": "test-pkce"}
    )


def _token_response():
    return httpx.Response(
        200, json={"access_token": "fresh-access", "refresh_token": "fresh-refresh"}
    )


def _reply():
    events = [
        {"type": "response.output_text.delta", "delta": "Recovered response."},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(f"data: {json.dumps(event)}\n\n" for event in events),
    )


@pytest.mark.parametrize("source", ["user", "reauth", "reconfigure"])
@pytest.mark.parametrize("stage", ["request", "poll", "exchange"])
@pytest.mark.parametrize("failure", ["transport", "invalid_response"])
async def test_flow_manager_auth_failure_can_recover(
    hass, auth_http, caplog, source, stage, failure
):
    queued, requests = auth_http
    assert await async_setup_component(hass, "homeassistant", {})
    context = {"source": source}
    entry = None
    if source != "user":
        entry = _entry()
        entry.add_to_hass(hass)
        context["entry_id"] = entry.entry_id
    original = dict(entry.data) if entry else None
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context=context, data=entry.data if source == "reauth" else None
    )
    start_step = "reauth_confirm" if source == "reauth" else source
    assert result["step_id"] == start_step
    failed_response = (
        httpx.ReadTimeout("private-auth-marker")
        if failure == "transport"
        else httpx.Response(200, json=["private-auth-marker"])
    )
    if stage == "request":
        queued.append((USER_CODE_PATH, failed_response))
    else:
        queued.append((USER_CODE_PATH, _device_response()))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    if stage != "request":
        assert result["step_id"] == "device"
        if stage == "poll":
            queued.append((POLL_PATH, failed_response))
        else:
            queued.extend([(POLL_PATH, _poll_response()), (TOKEN_PATH, failed_response)])
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == (start_step if stage == "request" else "device")
    assert result["errors"] == {
        "base": "device_code_request_failed" if stage == "request" else "device_code_auth_failed"
    }
    assert len(requests) == {"request": 1, "poll": 2, "exchange": 3}[stage]
    if entry:
        assert dict(entry.data) == original
    else:
        assert hass.config_entries.async_entries(DOMAIN) == []
    assert "private-auth-marker" not in caplog.text

    if stage == "request":
        queued.append((USER_CODE_PATH, _device_response()))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
        assert result["step_id"] == "device"
    queued.extend([(POLL_PATH, _poll_response()), (TOKEN_PATH, _token_response())])
    if source == "user":
        queued.append((MODEL_PATH, httpx.Response(200, json={"models": [{"slug": "gpt-5.4"}]})))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    if source == "user":
        assert result["step_id"] == "model"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"model": "gpt-5.4"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        entry = result["result"]
    else:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == f"{source}_successful"
    await hass.async_block_till_done()
    assert entry.data["access_token"] == "fresh-access"
    assert entry.data["refresh_token"] == "fresh-refresh"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("consumer", ["conversation", "ai_task"])
@pytest.mark.parametrize("mode", ["near_expiry", "expired", "rejected"])
async def test_public_request_recovers_after_temporary_refresh_failure(
    hass, auth_http, caplog, consumer, mode
):
    queued, requests = auth_http
    assert await async_setup_component(hass, "homeassistant", {})
    access = (
        "test-access" if mode == "rejected" else _jwt(0 if mode == "expired" else time.time() + 60)
    )
    entry = _entry(access)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    original = dict(entry.data)

    async def call():
        if consumer == "conversation":
            result = await conversation.async_converse(
                hass, "Respond briefly.", None, Context(), agent_id="conversation.codex_assist"
            )
            return result.response.speech["plain"]["speech"]
        result = await ai_task.async_generate_data(
            hass,
            task_name="Recovery",
            entity_id="ai_task.codex_assist_ai_task",
            instructions="Respond briefly.",
        )
        return result.data

    if mode == "rejected":
        queued.append((RESPONSE_PATH, httpx.Response(401)))
    queued.append((TOKEN_PATH, httpx.ConnectError("private-auth-marker")))
    if mode == "near_expiry":
        queued.append((RESPONSE_PATH, _reply()))
        assert await call() == "Recovered response."
    elif consumer == "ai_task":
        with pytest.raises(HomeAssistantError, match="Codex Assist AI Task failed") as caught:
            await call()
        assert "private-auth-marker" not in "".join(traceback.format_exception(caught.value))
    else:
        failure_text = await call()
        assert "Codex Assist failed" in failure_text
        assert "private-auth-marker" not in failure_text
    assert dict(entry.data) == original
    assert "private-auth-marker" not in caplog.text
    assert not hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, include_uninitialized=True
    )
    assert [request.url.path for request in requests] == {
        "near_expiry": [TOKEN_PATH, RESPONSE_PATH],
        "expired": [TOKEN_PATH],
        "rejected": [RESPONSE_PATH, TOKEN_PATH],
    }[mode]

    if mode == "rejected":
        queued.append((RESPONSE_PATH, httpx.Response(401)))
    queued.extend([(TOKEN_PATH, _token_response()), (RESPONSE_PATH, _reply())])
    assert await call() == "Recovered response."
    assert entry.data["access_token"] == "fresh-access"
    assert entry.data["refresh_token"] == "fresh-refresh"
    assert sum(request.url.path == TOKEN_PATH for request in requests) == 2
    assert not hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, include_uninitialized=True
    )


async def test_options_retains_cached_models_when_refresh_transport_fails(hass, auth_http, caplog):
    queued, requests = auth_http
    entry = _entry()
    entry.add_to_hass(hass)
    clock = [0]
    runtime_token_coordinator(entry).model_cache = ModelDiscoveryCache(clock=lambda: clock[0])
    queued.append((MODEL_PATH, httpx.Response(200, json={"models": [{"slug": "cached-model"}]})))
    assert (await async_entry_model_catalog(hass, entry)).source == "discovered"
    hass.config_entries.async_update_entry(entry, data={**entry.data, "access_token": _jwt(0)})
    original = dict(entry.data)
    clock[0] = 61
    queued.append((TOKEN_PATH, httpx.ReadTimeout("private-auth-marker")))

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    status = result["description_placeholders"]["model_status"]
    assert "cached list may be outdated" in status
    assert "fresh sign-in" not in status
    section = next(
        v for k, v in result["data_schema"].schema.items() if k.schema == SECTION_CHAT_SETTINGS
    )
    field, select = next((k, v) for k, v in section.schema.schema.items() if k.schema == "model")
    assert field.default() == "gpt-5.4"
    assert [option["value"] for option in select.config["options"]] == ["cached-model", "gpt-5.4"]
    assert select.config["options"][-1]["label"].endswith("(saved; not currently listed)")
    assert dict(entry.data) == original
    assert [request.url.path for request in requests] == [MODEL_PATH, TOKEN_PATH]
    assert "private-auth-marker" not in caplog.text
