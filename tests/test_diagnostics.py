from __future__ import annotations

import importlib
import json
import types
from copy import deepcopy

from tests.ha_fakes import install_homeassistant_fakes


def _load_diagnostics(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    return importlib.reload(
        importlib.import_module("custom_components.codex_assist.diagnostics")
    )


async def test_diagnostics_redacts_tokens_and_prompts_but_keeps_settings(monkeypatch):
    module = _load_diagnostics(monkeypatch)
    entry = types.SimpleNamespace(
        data={
            "access_token": "secret-access",
            "refresh_token": "secret-refresh",
            "model": "gpt-5.4",
            "prompt": "Synthetic data prompt marker.",
        },
        options={
            "access_token": "synthetic-option-access",
            "refresh_token": "synthetic-option-refresh",
            "prompt": "Synthetic options prompt marker.",
            "reasoning_effort": "low",
            "web_search": False,
        },
    )
    original_data = deepcopy(entry.data)
    original_options = deepcopy(entry.options)

    diagnostics = await module.async_get_config_entry_diagnostics(object(), entry)

    assert entry.data == original_data
    assert entry.options == original_options
    assert diagnostics == {
        "entry": {
            "data": {
                "access_token": "**REDACTED**",
                "refresh_token": "**REDACTED**",
                "model": "gpt-5.4",
                "prompt": "**REDACTED**",
            },
            "options": {
                "access_token": "**REDACTED**",
                "refresh_token": "**REDACTED**",
                "prompt": "**REDACTED**",
                "reasoning_effort": "low",
                "web_search": False,
            },
        },
    }
    serialized = json.dumps(diagnostics)
    for original in (original_data, original_options):
        for key in ("access_token", "refresh_token", "prompt"):
            assert original[key] not in serialized


async def test_diagnostics_never_contains_raw_token_values(monkeypatch):
    module = _load_diagnostics(monkeypatch)
    entry = types.SimpleNamespace(
        data={"access_token": "raw-token-value", "refresh_token": "raw-refresh-value"},
        options={},
    )

    diagnostics = await module.async_get_config_entry_diagnostics(object(), entry)

    assert "raw-token-value" not in str(diagnostics)
    assert "raw-refresh-value" not in str(diagnostics)
