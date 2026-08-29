import json
import re
import tomllib
from pathlib import Path


def test_manifest_is_hacs_loadable_custom_integration():
    manifest = json.loads(Path("custom_components/codex_assist/manifest.json").read_text())

    assert manifest["domain"] == "codex_assist"
    assert manifest["name"] == "Codex Assist"
    assert manifest["config_flow"] is True
    assert "conversation" in manifest["after_dependencies"]
    assert "ai_task" in manifest["after_dependencies"]
    # httpx is provided by Home Assistant core and should not be reinstalled by HACS.
    assert "requirements" not in manifest


def test_integration_declares_conversation_and_ai_task_platform_forwarding():
    init_py = Path("custom_components/codex_assist/__init__.py").read_text()

    assert "Platform.CONVERSATION" in init_py
    assert "Platform.AI_TASK" in init_py
    assert "async_forward_entry_setups" in init_py


def test_release_and_minimum_runtime_metadata_stay_aligned():
    manifest = json.loads(Path("custom_components/codex_assist/manifest.json").read_text())
    hacs = json.loads(Path("hacs.json").read_text())
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    minimum_requirements = Path("requirements_test_ha_min.txt").read_text()

    assert manifest["version"] == pyproject["project"]["version"]
    assert pyproject["project"]["requires-python"] == ">=3.14"
    assert f"homeassistant=={hacs['homeassistant']}" in minimum_requirements


def test_ci_actions_are_pinned_to_immutable_commits():
    workflow = Path(".github/workflows/ci.yml").read_text()
    action_refs = re.findall(r"^\s*uses:\s+[^@\s]+@([^\s#]+)", workflow, flags=re.MULTILINE)

    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
