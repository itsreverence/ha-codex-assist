from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
            "TEST-CODE", "test-device", 5, "https://auth.openai.com/codex/device"
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
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Codex Assist",
        data={"access_token": "old-access", "refresh_token": "old-refresh", "model": "saved-model"},
        options={"web_search": True},
    )
    entry.add_to_hass(hass)
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
    assert dict(entry.data) == {
        "access_token": "new-access", "refresh_token": "new-refresh", "model": "saved-model"
    }
    assert dict(entry.options) == {"web_search": True}
    reload_entry.assert_awaited_once_with(entry.entry_id)
