from __future__ import annotations

import importlib
import types

from tests.ha_fakes import install_homeassistant_fakes


def _load_diagnostics(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    return importlib.reload(
        importlib.import_module("custom_components.codex_assist.diagnostics")
    )


def _entry(**subentries):
    return types.SimpleNamespace(
        data={"access_token": "raw-token-value", "refresh_token": "raw-refresh-value"},
        options={},
        subentries=subentries,
    )


async def test_diagnostics_redacts_tokens_but_keeps_agent_settings(monkeypatch):
    module = _load_diagnostics(monkeypatch)
    entry = _entry(
        abc=types.SimpleNamespace(
            subentry_type="conversation",
            title="Kitchen agent",
            data={"model": "gpt-5.4", "prompt": "You are helpful."},
        )
    )

    diagnostics = await module.async_get_config_entry_diagnostics(object(), entry)

    assert diagnostics["entry"]["data"] == {
        "access_token": module.REDACTED,
        "refresh_token": module.REDACTED,
    }
    assert diagnostics["subentries"] == [
        {
            "subentry_type": "conversation",
            "title": "Kitchen agent",
            "data": {"model": "gpt-5.4", "prompt": "You are helpful."},
        }
    ]


async def test_diagnostics_never_contains_raw_token_values(monkeypatch):
    module = _load_diagnostics(monkeypatch)

    diagnostics = await module.async_get_config_entry_diagnostics(object(), _entry())

    assert "raw-token-value" not in str(diagnostics)
    assert "raw-refresh-value" not in str(diagnostics)
