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


@pytest.mark.asyncio
async def test_probe_rejects_completed_items_without_arguments() -> None:
    module = _probe_module()
    item = {"id": "fc_0", "type": "function_call", "call_id": "call_0", "name": "record_word"}
    events = [
        {"type": "response.function_call_arguments.done", "item_id": "fc_0",
         "arguments": '{"word":"alpha"}'},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()

    summary = await module.run_probe(
        model="gpt-test", access_token="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )

    assert summary["result"] == "inconsistent"
    assert summary["items_have_object_arguments"] is False
