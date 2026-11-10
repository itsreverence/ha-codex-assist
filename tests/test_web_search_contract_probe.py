from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def _probe_module():
    path = Path(__file__).parents[1] / "scripts" / "probe_web_search_contract.py"
    spec = importlib.util.spec_from_file_location("probe_web_search_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_payload_is_fixed_and_data_minimized() -> None:
    module = _probe_module()

    payload = module.probe_payload("gpt-test")

    assert payload["model"] == "gpt-test"
    assert payload["tools"] == [
        {
            "type": "web_search",
            "external_web_access": True,
            "filters": {"allowed_domains": ["iana.org"]},
            "search_context_size": "low",
        }
    ]
    assert payload["include"] == ["web_search_call.action.sources"]
    assert payload["store"] is False
    assert all("attachments" not in item for item in payload["input"])
    assert all("entity_id" not in item for item in payload["input"])


def test_probe_requires_explicit_model(monkeypatch, capsys) -> None:
    module = _probe_module()
    monkeypatch.setattr("sys.argv", ["probe", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert "--model" in capsys.readouterr().err


def test_probe_dry_run_uses_selected_model(monkeypatch, capsys) -> None:
    module = _probe_module()
    monkeypatch.setattr("sys.argv", ["probe", "--dry-run", "--model", "account-model"])
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["model"] == "account-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [
    [{"type": "response.failed", "response": {
        "error": {"code": "server_error", "message": "private-backend-detail"},
    }}],
    [{"type": "response.output_text.delta", "delta": "private-answer"}],
    [{"type": "response.completed", "response": {"status": "completed"}}],
    [{"type": "response.incomplete", "response": {
        "incomplete_details": {"reason": "private-detail"},
    }}],
    [{"type": "error", "message": "private-detail"}],
    [
        {"type": "response.output_item.done", "item": {
            "type": "web_search_call", "status": "completed",
        }},
        {"type": "response.output_text.delta", "delta": "private-answer"},
        {"type": "response.failed"},
    ],
    [{"type": "response.completed", "response": {"output": [
        {"type": "web_search_call", "status": "in_progress"},
        {"type": "message", "content": [
            {"type": "output_text", "text": "private-answer"},
        ]},
    ]}}],
    [{"type": "response.completed", "response": {"output": [
        {"type": "web_search_call", "status": "completed"},
    ]}}],
    [{"type": "response.completed", "response": {"output": [
        {"type": "web_search_call", "status": "completed"},
        {"type": "web_search_call", "status": "failed"},
        {"type": "message", "content": [
            {"type": "output_text", "text": "private-answer"},
        ]},
    ]}}],
])
async def test_probe_rejects_failed_truncated_or_searchless_responses(
    events, monkeypatch, capsys,
) -> None:
    module = _probe_module()
    stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=stream))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: client_type(
        transport=transport, **kw,
    ))

    assert await module.run_probe(model="gpt-test", access_token="private-token") == 1
    assert "private" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_probe_accepts_completed_search_and_answer(monkeypatch, capsys) -> None:
    module = _probe_module()
    event = {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "web_search_call", "status": "completed", "id": "private-id"},
        {"type": "message", "content": [
            {"type": "output_text", "text": "private-answer"},
        ]},
    ]}}
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text=f"data: {json.dumps(event)}\n\n",
    ))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: client_type(
        transport=transport, **kw,
    ))

    assert await module.run_probe(model="gpt-test", access_token="private-token") == 0
    assert json.loads(capsys.readouterr().out) == {
        "result": "completed", "searched": True, "answered": True,
    }
