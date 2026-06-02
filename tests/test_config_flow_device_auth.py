from pathlib import Path

CONFIG_FLOW = Path("custom_components/codex_assist/config_flow.py")


def test_config_flow_has_device_code_pairing_steps():
    source = CONFIG_FLOW.read_text()

    assert "async_step_device" in source
    assert "async_step_device_wait" in source
    assert "CODEX_DEVICE_VERIFICATION_URL" in source
    assert "authorization_pending" in source


def test_config_flow_creates_entry_only_after_token_exchange():
    source = CONFIG_FLOW.read_text()

    assert "exchange_authorization_code" in source
    assert '"access_token": tokens.access_token' in source
    assert '"refresh_token": tokens.refresh_token' in source
    assert "async_create_entry" in source
