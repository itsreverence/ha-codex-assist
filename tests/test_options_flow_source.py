from pathlib import Path

CONFIG_FLOW = Path("custom_components/codex_assist/config_flow.py")


def test_config_flow_exposes_options_flow_for_safe_runtime_settings():
    source = CONFIG_FLOW.read_text()

    assert "async_get_options_flow" in source
    assert "CodexAssistOptionsFlow" in source
    assert "async_step_init" in source
    assert "async_create_entry" in source


def test_options_flow_keeps_safety_mode_talk_only_by_default():
    source = CONFIG_FLOW.read_text()

    assert "SAFETY_MODE_TALK_ONLY" in source
    assert "DEFAULT_SAFETY_MODE" in source
    assert "exposed_entities_only" not in source
    assert "scripts_only" not in source
