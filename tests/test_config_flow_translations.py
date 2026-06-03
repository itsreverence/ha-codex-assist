import json
from pathlib import Path

TRANSLATION = Path("custom_components/codex_assist/translations/en.json")


def test_config_flow_device_step_has_visible_translation_description():
    assert TRANSLATION.exists()

    data = json.loads(TRANSLATION.read_text())
    device_step = data["config"]["step"]["device"]

    assert device_step["title"] == "Sign in with Codex"
    assert "{verification_uri}" in device_step["description"]
    assert "{user_code}" in device_step["description"]
    assert "{interval}" in device_step["description"]


def test_config_flow_errors_are_translated_for_device_polling():
    data = json.loads(TRANSLATION.read_text())
    errors = data["config"]["error"]

    assert "authorization_pending" in errors
    assert "device_code_auth_failed" in errors
    assert "device_code_request_failed" in errors
