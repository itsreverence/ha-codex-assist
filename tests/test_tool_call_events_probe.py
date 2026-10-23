from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def _probe_module():
    path = Path(__file__).parents[1] / "scripts" / "probe_tool_call_events.py"
    spec = importlib.util.spec_from_file_location("probe_tool_call_events", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_probe_accepts_a_backend_shaped_stream() -> None:
    module = _probe_module()

    summary = await module.run_probe(
        model="gpt-test", access_token="test", transport=module.synthetic_transport()
    )

    assert summary["result"] == "consistent"
    assert summary["function_call_items"] == summary["client_tool_calls"] == 2


_ITEM = {"id": "fc_0", "type": "function_call", "call_id": "call_0", "name": "record_word"}
_ALPHA = '{"word":"alpha"}'


@pytest.mark.asyncio
@pytest.mark.parametrize(("events", "failed_check"), [
    ([{"type": "response.output_item.done", "item": _ITEM}], "items_have_object_arguments"),
    ([{"type": "response.function_call_arguments.done", "item_id": "fc_1", "arguments": _ALPHA},
      {"type": "response.output_item.done", "item": {**_ITEM, "arguments": _ALPHA}}],
     "announced_calls_completed"),
    ([{"type": "response.output_item.done", "item": {**_ITEM, "arguments": _ALPHA}},
      {"type": "response.output_item.done",
       "item": {**_ITEM, "id": "fc_1", "arguments": _ALPHA}}],
     "call_ids_unique"),
    ([{"type": "response.function_call_arguments.done", "item_id": "fc_0",
       "arguments": '{"word":"beta"}'},
      {"type": "response.output_item.done", "item": {**_ITEM, "arguments": _ALPHA}}],
     "arguments_done_match_items"),
], ids=["no-arguments", "call-never-completed", "repeated-call", "arguments-disagree"])
async def test_probe_reports_streams_the_client_cannot_trust(events, failed_check) -> None:
    module = _probe_module()
    events = [*events, {"type": "response.completed", "response": {"status": "completed"}}]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()

    summary = await module.run_probe(
        model="gpt-test", access_token="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )

    assert summary["result"] == "inconsistent"
    assert summary[failed_check] is False


def test_live_probe_reports_failures_by_type_only(monkeypatch, capsys) -> None:
    module = _probe_module()
    monkeypatch.setenv("CODEX_ASSIST_ACCESS_TOKEN", "test")

    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic_private_detail")

    monkeypatch.setattr(module, "resolve_model", fail)
    monkeypatch.setattr("sys.argv", ["probe", "--model", "gpt-test"])

    assert module.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"result": "error", "error_type": "RuntimeError"}
    assert "synthetic_private_detail" not in output.out + output.err
