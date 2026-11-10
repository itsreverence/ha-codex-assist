"""Probe wiring tests use only synthetic HTTP; they are not live search proof."""
from __future__ import annotations

import json

import httpx
import pytest

from scripts import probe_client_search as probe


def sse(*events):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content="".join(f"data: {json.dumps(event)}\n\n" for event in events),
    )


def answer(text):
    return [
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.output_item.done", "item": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]


async def test_probe_exercises_search_open_and_replay_without_printing_content():
    captured = []
    replies = [
        httpx.Response(200, json={"output": "private-result-sentinel"}),
        httpx.Response(200, json={"output": "private-page-sentinel"}),
        sse({"type": "response.output_item.done", "item": {
            "type": "function_call", "name": "web_search", "call_id": "s1",
            "arguments": '{"queries":["private-query-sentinel"]}',
        }}, {"type": "response.completed", "response": {"status": "completed"}}),
        httpx.Response(200, json={"output": "private-result-sentinel"}),
        sse(*answer("private-answer-sentinel")),
        sse(*answer("private-followup-sentinel")),
    ]

    def respond(request):
        captured.append((request.url.path, json.loads(request.content)))
        assert request.headers["authorization"] == "Bearer private-token-sentinel"
        return replies.pop(0)

    result = await probe.probe(
        "gpt-test", "private-token-sentinel", transport=httpx.MockTransport(respond),
    )
    assert result["result"] == "completed"
    assert result["searched"] and result["answered"] and result["followup_answered"]
    assert result["semantic_correctness"] == "not_assessed"
    assert len(result["requests"]) == 6
    assert not replies
    assert captured[0][1]["commands"] == {"search_query": [{"q": "site:iana.org reserved domains"}]}
    assert captured[1][1]["commands"] == {
        "open": [{"ref_id": "https://www.iana.org/help/example-domains"}],
    }
    assert "private-answer-sentinel" in json.dumps(captured[-1][1]["input"])
    assert "private-" not in json.dumps(result)


async def test_probe_does_not_claim_model_search_when_only_direct_probe_searched():
    replies = [httpx.Response(200, json={"output": "result"}) for _ in range(2)]
    replies.append(sse(*answer("An answer from memory.")))
    result = await probe.probe(
        "gpt-test", "synthetic-token",
        transport=httpx.MockTransport(lambda request: replies.pop(0)),
    )
    assert result["result"] == "model_search_not_established"
    assert result["searched"] is False
    assert not replies


@pytest.mark.parametrize("failure", [
    httpx.Response(500, json={"error": {"message": "private-error"}}),
    httpx.Response(200, json={"output": None}),
    httpx.Response(200, json={"output": "  "}),
    httpx.Response(200, content="private-invalid-json"),
])
@pytest.mark.parametrize("earlier_success", [False, True])
async def test_probe_rejects_failed_model_search_even_with_answers(
    monkeypatch, capsys, failure, earlier_success,
):
    replies = [httpx.Response(200, json={"output": "public-result"}) for _ in range(2)]
    for number in range(2 if earlier_success else 1):
        replies.append(sse({"type": "response.output_item.done", "item": {
            "type": "function_call", "name": "web_search", "call_id": f"s{number}",
            "arguments": '{"queries":["private-query"]}',
        }}, {"type": "response.completed", "response": {"status": "completed"}}))
        replies.append(
            httpx.Response(200, json={"output": "private-good-result"})
            if earlier_success and number == 0 else failure
        )
    replies.extend([sse(*answer("private-answer")), sse(*answer("private-followup"))])
    actual_probe = probe.probe

    async def offline(model, token):
        return await actual_probe(
            model, token, transport=httpx.MockTransport(lambda request: replies.pop(0)),
        )

    monkeypatch.setattr(probe, "probe", offline)
    assert await probe.run("gpt-test", "synthetic-token") == 1
    output = capsys.readouterr().out
    assert json.loads(output)["result"] == "model_search_not_established"
    assert "private-" not in output
    assert len(replies) == 1, "Do not proceed to replay after unsuccessful model search"


async def test_probe_error_output_does_not_leak_backend_exception(monkeypatch, capsys):
    async def broken(*args):
        raise RuntimeError("private-backend-content-and-token")

    monkeypatch.setattr(probe, "probe", broken)
    assert await probe.run("gpt-test", "synthetic-token") == 1
    assert json.loads(capsys.readouterr().out) == {"result": "failed", "error_type": "RuntimeError"}


@pytest.mark.parametrize("dry_run", [False, True])
def test_probe_cli_never_runs_without_explicit_integration_token(monkeypatch, capsys, dry_run):
    monkeypatch.delenv("CODEX_ASSIST_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["probe", "--model", "gpt-test"] + (
        ["--dry-run"] if dry_run else []
    ))
    assert probe.main() == (0 if dry_run else 2)
    output = capsys.readouterr()
    if dry_run:
        assert json.loads(output.out)["live_request_sent"] is False
    else:
        assert "live probe not run" in output.err
