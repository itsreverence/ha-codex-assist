from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from codex_entries import codex_entry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.codex_assist import DOMAIN
from custom_components.codex_assist.codex_auth import (
    CodexAuthorizationCode,
    CodexDeviceCode,
    CodexTokenSet,
)


@pytest.fixture
def auth(monkeypatch):
    client = SimpleNamespace(
        request_device_code=AsyncMock(return_value=CodexDeviceCode(
            user_code="TEST-CODE", device_auth_id="test-device",
            verification_uri="https://auth.openai.com/codex/device", interval=5,
        )),
        poll_device_code=AsyncMock(return_value=CodexAuthorizationCode("test-code", "verifier")),
        exchange_authorization_code=AsyncMock(
            return_value=CodexTokenSet("new-access", "new-refresh")
        ),
    )
    monkeypatch.setattr(
        "custom_components.codex_assist.config_flow.CodexAssistConfigFlow._auth_client",
        lambda self: client,
    )
    return client


@pytest.fixture
async def entry(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = codex_entry(
        {"access_token": "old-access", "refresh_token": "old-refresh"},
        conversation={"model": "saved-model", "web_search": True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_duplicate_setup_aborts_before_sign_in(hass, entry, auth):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
    auth.request_device_code.assert_not_awaited()
    auth.poll_device_code.assert_not_awaited()
    auth.exchange_authorization_code.assert_not_awaited()
    assert hass.config_entries.async_entries(DOMAIN) == [entry]


@pytest.mark.parametrize("source", ["reauth", "reconfigure"])
async def test_existing_entry_can_reconnect(hass, entry, auth, monkeypatch, source):
    reload_entry = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
    unloads = []
    entry.async_on_unload(lambda: unloads.append(entry.entry_id))
    agents = {key: dict(value.data) for key, value in entry.subentries.items()}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": source, "entry_id": entry.entry_id},
        data=entry.data if source == "reauth" else None,
    )
    assert result["step_id"] == ("reauth_confirm" if source == "reauth" else "reconfigure")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result["step_id"] == "device"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == f"{source}_successful"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert dict(entry.data) == {"access_token": "new-access", "refresh_token": "new-refresh"}
    assert {key: dict(value.data) for key, value in entry.subentries.items()} == agents
    # Entities read credentials per request, so new tokens apply without a reload.
    reload_entry.assert_not_awaited()
    assert unloads == []
    assert entry.state is ConfigEntryState.LOADED
