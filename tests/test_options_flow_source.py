from pathlib import Path

CONFIG_FLOW = Path("custom_components/codex_assist/config_flow.py")


def test_config_flow_exposes_options_flow_for_runtime_settings():
    source = CONFIG_FLOW.read_text()

    assert "async_get_options_flow" in source
    assert "CodexAssistOptionsFlow" in source
    assert "async_step_init" in source
    assert "async_create_entry" in source


def test_options_flow_uses_codex_model_selector_with_custom_slug_fallback():
    source = CONFIG_FLOW.read_text()

    assert "fetch_codex_model_ids" in source
    assert "SelectSelector" in source
    assert "SelectSelectorMode.DROPDOWN" in source
    assert "custom_value=True" in source


def test_options_flow_uses_full_control_as_the_only_control_mode():
    source = CONFIG_FLOW.read_text()

    assert "SAFETY_MODE_FULL_CONTROL" in source
    assert "DEFAULT_SAFETY_MODE = SAFETY_MODE_FULL_CONTROL" in source
    assert "safety_mode = SAFETY_MODE_FULL_CONTROL" in source
    assert "SAFETY_MODE_TALK_ONLY" not in source
    assert "exposed_entities_only" not in source
    assert "scripts_only" not in source
