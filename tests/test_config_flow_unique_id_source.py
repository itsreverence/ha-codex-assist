from pathlib import Path

CONFIG_FLOW = Path("custom_components/codex_assist/config_flow.py")


def _method_source(source: str, method_name: str) -> str:
    start = source.index(f"    async def {method_name}")
    next_method = source.find("    async def ", start + 1)
    next_sync = source.find("    def ", start + 1)
    candidates = [idx for idx in [next_method, next_sync] if idx != -1]
    end = min(candidates) if candidates else len(source)
    return source[start:end]


def test_config_flow_does_not_reserve_unique_id_before_device_pairing():
    source = CONFIG_FLOW.read_text()
    user_step = _method_source(source, "async_step_user")

    assert "async_set_unique_id" not in user_step
    assert "_abort_if_unique_id_configured" not in user_step
    assert "request_device_code" in user_step


def test_config_flow_checks_duplicate_entry_only_before_creating_entry():
    source = CONFIG_FLOW.read_text()
    device_wait_step = _method_source(source, "async_step_device_wait")

    assert "async_set_unique_id" in device_wait_step
    assert "_abort_if_unique_id_configured" in device_wait_step
    assert device_wait_step.index("async_set_unique_id") < device_wait_step.index(
        "async_create_entry"
    )
