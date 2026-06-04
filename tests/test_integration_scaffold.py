import json
from pathlib import Path


def test_manifest_is_hacs_loadable_custom_integration():
    manifest = json.loads(Path("custom_components/codex_assist/manifest.json").read_text())

    assert manifest["domain"] == "codex_assist"
    assert manifest["name"] == "Codex Assist"
    assert manifest["config_flow"] is True
    assert "conversation" in manifest["after_dependencies"]
    assert "ai_task" in manifest["after_dependencies"]
    assert "httpx" in " ".join(manifest["requirements"])


def test_integration_declares_conversation_and_ai_task_platform_forwarding():
    init_py = Path("custom_components/codex_assist/__init__.py").read_text()

    assert "Platform.CONVERSATION" in init_py
    assert "Platform.AI_TASK" in init_py
    assert "async_forward_entry_setups" in init_py
