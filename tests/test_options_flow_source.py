from pathlib import Path

CONFIG_FLOW = Path("custom_components/codex_assist/config_flow.py")


def test_config_flow_exposes_options_flow_for_runtime_settings():
    source = CONFIG_FLOW.read_text()

    assert "async_get_options_flow" in source
    assert "CodexAssistOptionsFlow" in source
    assert "async_step_init" in source
    assert "async_create_entry" in source
    assert "return CodexAssistOptionsFlow()" in source
    assert "self.config_entry = config_entry" not in source


def test_options_flow_uses_codex_model_selector_with_custom_slug_fallback():
    source = CONFIG_FLOW.read_text()

    assert "fetch_codex_model_ids" in source
    assert "SelectSelector" in source
    assert "SelectSelectorMode.DROPDOWN" in source
    assert "custom_value=True" in source


def test_options_flow_does_not_expose_redundant_safety_mode_choice():
    source = CONFIG_FLOW.read_text()

    assert "CONF_SAFETY_MODE" not in source
    assert "safety_mode" not in source
    assert "full_control" not in source
    assert "exposed_entities_only" not in source
    assert "scripts_only" not in source
