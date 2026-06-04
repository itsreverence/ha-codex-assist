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


def test_options_flow_uses_codex_model_selector_without_custom_slug_footgun():
    source = CONFIG_FLOW.read_text()

    assert "fetch_codex_model_ids" in source
    assert "SelectSelector" in source
    assert "SelectSelectorMode.DROPDOWN" in source
    assert "custom_value=True" not in source


def test_options_flow_exposes_advanced_codex_response_controls():
    source = CONFIG_FLOW.read_text()

    assert "CONF_REASONING_EFFORT" in source
    assert "CONF_REASONING_SUMMARY" in source
    assert "CONF_TEXT_VERBOSITY" in source
    assert '["low", "medium", "high"]' in source
    assert '["auto", "concise", "detailed", "off"]' in source


def test_options_flow_exposes_image_generation_controls():
    source = CONFIG_FLOW.read_text()

    assert "CONF_IMAGE_MODEL" in source
    assert "CONF_IMAGE_SIZE" in source
    assert "gpt-image-2-low" in source
    assert "gpt-image-2-medium" in source
    assert "gpt-image-2-high" in source
    assert "1024x1024" in source
    assert "1536x1024" in source
    assert "1024x1536" in source


def test_options_flow_does_not_expose_redundant_safety_mode_choice():
    source = CONFIG_FLOW.read_text()

    assert "CONF_SAFETY_MODE" not in source
    assert "safety_mode" not in source
    assert "full_control" not in source
    assert "exposed_entities_only" not in source
    assert "scripts_only" not in source
