"""Synthetic HTTP/SSE exercises the real client without live search or credentials."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from scripts import probe_search_comparison as probe

COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}
REASONING = {"type": "reasoning", "encrypted_content": "private-reasoning-sentinel"}


def item(value):
    return {"type": "response.output_item.done", "item": value}


def encode(events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def sse(*events):
    return httpx.Response(200, content=encode(events))


def answer(text="private-answer-sentinel"):
    return [
        {"type": "response.output_text.delta", "delta": text},
        item({"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text, "annotations": []},
        ]}),
        COMPLETED,
    ]


def search_call(number=1, *, arguments=None, name="web_search"):
    return {
        "type": "function_call", "name": name, "id": f"private-item-{number}",
        "call_id": f"private-call-{number}",
        "arguments": json.dumps(arguments if arguments is not None else {
            "queries": ["private-query-sentinel"],
        }),
    }


def builtin_search(*, status="completed", action=None):
    return {
        "type": "web_search_call", "id": "private-search-id", "status": status,
        "action": action or {"type": "search", "queries": ["private-query-sentinel"]},
    }


async def trial(arm, replies, *, case=None):
    captured = []

    def respond(request):
        captured.append((request.url.path, json.loads(request.content)))
        assert request.headers["authorization"] == "Bearer private-token-sentinel"
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    case = case or probe.CASES[0]
    result = probe.TrialResult(case.name, arm)
    await probe.run_trial(
        result, case, "gpt-test", "private-token-sentinel", transport=httpx.MockTransport(respond),
    )
    return result, captured


@pytest.mark.parametrize("arm", ["client", "builtin"])
async def test_success_replays_complete_native_evidence_not_visible_answer(arm):
    call = search_call(arguments={"queries": [], "open_urls": [probe.DIRECT_URL]})
    opened = builtin_search(action={"type": "open_page", "url": probe.DIRECT_URL})
    hidden = "private-evidence-never-in-visible-answer"
    if arm == "client":
        replies = [
            sse(item(REASONING), item(call), COMPLETED),
            httpx.Response(200, json={"output": hidden}),
            sse(*answer()), sse(*answer("private-followup")),
        ]
        expected_native = [
            REASONING, call,
            {"type": "function_call_output", "call_id": call["call_id"], "output": hidden},
            answer()[1]["item"],
        ]
    else:
        source = {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": hidden},
        ]}
        replies = [
            sse(item(REASONING), item(opened), item(source), *answer()),
            sse(*answer("private-followup")),
        ]
        expected_native = [REASONING, opened, source, answer()[1]["item"]]
    result, captured = await trial(arm, replies, case=probe.CASES[2])
    assert result.status == "completed"
    assert result.gate_pass
    assert result.initial.response_complete and result.followup.response_complete
    assert result.initial.answered and result.followup.answered
    assert result.initial.search_calls == result.initial.search_completed == 1
    assert result.initial.open_target_completed
    assert captured[-1][1]["input"] == [
        {"role": "user", "content": probe.CASES[2].prompt},
        *expected_native, {"role": "user", "content": probe.FOLLOWUP},
    ]
    assert "tools" not in captured[-1][1]
    assert result.initial.first_visible_text_seconds is not None
    assert result.initial.total_seconds >= result.initial.first_visible_text_seconds
    assert result.total_seconds >= result.initial.total_seconds
    assert not replies
    assert "private-" not in json.dumps(probe.asdict(result))


async def test_effective_payload_parity_and_actual_routing():
    client, client_requests = await trial("client", [
        sse(item(search_call()), COMPLETED),
        httpx.Response(200, json={"output": "private-search-output"}),
        sse(*answer()), sse(*answer()),
    ])
    builtin, builtin_requests = await trial("builtin", [
        sse(item(builtin_search()), *answer()), sse(*answer()),
    ])
    assert client.gate_pass and builtin.gate_pass
    client_payload = client_requests[0][1]
    builtin_payload = builtin_requests[0][1]
    for key in ("model", "instructions", "input", "stream", "store"):
        assert client_payload[key] == builtin_payload[key]
    assert client_payload["tools"][0]["type"] == "function"
    assert client_payload["tools"][0]["name"] == "web_search"
    assert builtin_payload["tools"] == [{
        "type": "web_search", "search_context_size": "medium", "external_web_access": True,
    }]
    assert client_requests[1][0].endswith("/alpha/search")
    assert client_requests[1][1]["model"] == "gpt-test"
    assert client_requests[1][1]["settings"] == {
        "search_context_size": "medium", "external_web_access": True,
    }
    assert client_requests[1][1]["commands"] == {
        "search_query": [{"q": "private-query-sentinel"}],
    }
    assert all(path.endswith("/responses") for path, _ in builtin_requests)
    assert client.initial.http_requests == 3
    assert client.initial.search_requests == 1
    assert builtin.initial.http_requests == 1


@pytest.mark.parametrize("failure", [
    httpx.Response(500, json={"error": {"message": "private-error"}}),
    httpx.Response(200, content="private-invalid-json"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"output": None}),
    httpx.Response(200, json={"output": 23}),
    httpx.Response(200, json={"output": "  "}),
    httpx.Response(200, json={"output": "[wordlim: 2] "}),
    httpx.Response(200, json={"output": "Web search failed private-sentinel"}),
    httpx.Response(200, json={"output": "Web search returned private-sentinel"}),
    httpx.Response(200, json={"output": "Web search found nothing private-sentinel"}),
])
@pytest.mark.parametrize("earlier_success", [False, True])
async def test_all_swallowed_failures_fail_gate_even_after_earlier_success(
    failure, earlier_success,
):
    replies = []
    if earlier_success:
        replies.extend([
            sse(item(search_call(1)), COMPLETED),
            httpx.Response(200, json={"output": "private-good-result"}),
        ])
    replies.extend([sse(item(search_call(2)), COMPLETED), failure, sse(*answer())])
    result, requests = await trial("client", replies)
    assert result.status == "search_not_established"
    assert result.initial.response_complete and result.initial.answered
    assert result.initial.search_calls == (2 if earlier_success else 1)
    assert result.initial.search_completed == int(earlier_success)
    assert result.initial.search_requests == result.initial.search_calls
    assert not result.gate_pass and not result.followup.started
    assert len(requests) == (5 if earlier_success else 3)
    assert "private-" not in json.dumps(probe.asdict(result))
    assert not replies


async def test_invalid_client_arguments_without_http_are_not_search_success():
    result, requests = await trial("client", [
        sse(item(search_call(arguments={"queries": []})), COMPLETED), sse(*answer()),
    ])
    assert result.status == "search_not_established"
    assert result.initial.search_calls == 1 and result.initial.search_completed == 0
    assert result.initial.search_requests == 0
    assert len(requests) == 2


@pytest.mark.parametrize("items", [
    [], [builtin_search(status="failed")], [builtin_search(status="incomplete")],
    [builtin_search(status="in_progress")], [builtin_search(status="private-status")],
    [builtin_search(), builtin_search(status="failed")],
])
async def test_builtin_requires_every_search_item_completed_not_just_answer_or_citation(items):
    events = [item(value) for value in items]
    events.append({"type": "response.output_text.annotation.added", "annotation": {
        "type": "url_citation", "url": probe.DIRECT_URL, "title": "private-title",
    }})
    result, requests = await trial("builtin", [sse(*events, *answer())])
    assert result.status == "search_not_established"
    assert result.initial.response_complete and result.initial.answered
    assert not result.gate_pass and not result.followup.started
    assert len(requests) == 1
    assert "private-" not in json.dumps(probe.asdict(result))


@pytest.mark.parametrize("action", [
    {"type": "search", "queries": [probe.DIRECT_URL]},
    {"type": "open", "url": probe.DIRECT_URL},
    {"type": "private-unknown", "url": probe.DIRECT_URL},
    {"type": "open_page", "url": "https://example.org/private-target"},
    {"type": "open_page"},
])
async def test_builtin_unknown_or_wrong_open_is_unestablished(action):
    result, _ = await trial("builtin", [
        sse(item(builtin_search(action=action)), *answer()),
    ], case=probe.CASES[2])
    assert result.status == "open_not_established"
    assert result.initial.search_completed == 1
    assert not result.initial.open_target_completed
    assert not result.followup.started
    assert "private-" not in json.dumps(probe.asdict(result))


async def test_client_direct_case_requires_successful_actual_target_open():
    result, _ = await trial("client", [
        sse(item(search_call(arguments={"queries": [probe.DIRECT_URL]})), COMPLETED),
        httpx.Response(200, json={"output": "private-result"}), sse(*answer()),
    ], case=probe.CASES[2])
    assert result.status == "open_not_established"
    assert not result.initial.open_target_completed


@pytest.mark.parametrize("urls,established", [
    (["", "", "", probe.DIRECT_URL], True),
    (["https://example.org/a", "https://example.org/b", "https://example.org/c",
      probe.DIRECT_URL], False),
])
async def test_client_open_gate_observes_effective_http_commands(urls, established):
    replies = [
        sse(item(search_call(arguments={"queries": [], "open_urls": urls})), COMPLETED),
        httpx.Response(200, json={"output": "private-result"}), sse(*answer()),
    ]
    if established:
        replies.append(sse(*answer()))
    result, captured = await trial("client", replies, case=probe.CASES[2])
    assert result.initial.open_target_completed is established
    assert result.gate_pass is established
    assert any(command["ref_id"] == probe.DIRECT_URL
               for command in captured[1][1]["commands"]["open"]) is established


@pytest.mark.parametrize("ending", [
    [], [{"type": "response.failed", "response": {"error": {"message": "private-failure"}}}],
    [{"type": "response.incomplete", "response": {
        "incomplete_details": {"reason": "private-reason"},
    }}],
])
@pytest.mark.parametrize("arm", ["client", "builtin"])
async def test_eof_or_failure_is_not_completion_and_retains_partial_evidence(arm, ending):
    result, requests = await trial(arm, [sse(
        item(REASONING), item(builtin_search()), *answer()[:-1], *ending,
    )])
    assert result.status == "error" and result.error == "runtime"
    assert not result.initial.response_complete and not result.gate_pass
    assert result.initial.http_requests == 1 and not result.followup.started
    if arm == "builtin":
        assert result.initial.answered and result.initial.native_items == 3
        assert result.initial.search_completed == 1
    assert len(requests) == 1
    assert "private-" not in json.dumps(probe.asdict(result))


@pytest.mark.parametrize("arm", ["client", "builtin"])
@pytest.mark.parametrize("followup", [False, True])
async def test_unexpected_functions_never_execute(arm, followup):
    bad = sse(item(search_call(name="private-unexpected-function")), *answer())
    replies = [bad]
    if followup:
        replies = [sse(item(builtin_search()), *answer()), bad] if arm == "builtin" else [
            sse(item(search_call()), COMPLETED),
            httpx.Response(200, json={"output": "private-result"}), sse(*answer()), bad,
        ]
    result, requests = await trial(arm, replies)
    assert result.status == "unexpected_tool" and not result.gate_pass
    evidence = result.followup if followup else result.initial
    assert evidence.unexpected_tool_calls == 1
    assert len(requests) == (4 if followup and arm == "client" else 2 if followup else 1)


@pytest.mark.parametrize("arm", ["client", "builtin"])
async def test_empty_followup_fails_gate(arm):
    replies = [sse(item(builtin_search()), *answer())] if arm == "builtin" else [
        sse(item(search_call()), COMPLETED),
        httpx.Response(200, json={"output": "private-result"}), sse(*answer()),
    ]
    result, _ = await trial(arm, [*replies, sse(*answer(" "))])
    assert result.status == "followup_answer_not_established"
    assert result.followup.response_complete and not result.followup.answered
    assert not result.gate_pass


class GatedStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        yield encode([item(builtin_search()), *answer()[:-1]])
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("deadline", ["turn", "arm", "overall"])
@pytest.mark.parametrize("arm", ["client", "builtin"])
async def test_deadlines_close_streams_and_keep_partial_json(deadline, arm, monkeypatch):
    stream = GatedStream()
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(probe, "TURN_SECONDS", 0.01 if deadline == "turn" else 10)
    monkeypatch.setattr(probe, "ARM_SECONDS", 0.01 if deadline == "arm" else 20)
    monkeypatch.setattr(probe, "TOTAL_SECONDS", 0.01 if deadline == "overall" else 30)
    transport = httpx.MockTransport(respond)
    if deadline == "overall":
        report, code = await probe.compare("gpt-test", "test", arm, transport=transport)
        assert code == 3 and report["status"] == "incomplete"
        assert report["trials"][0]["error"] == "overall_timeout"
        assert all(t["status"] == "not_run" for t in report["trials"][1:])
        assert "private-" not in json.dumps(report)
    else:
        result = probe.TrialResult(probe.CASES[0].name, arm)
        await probe.run_trial(result, probe.CASES[0], "gpt-test", "test", transport=transport)
        assert result.error == "timeout" and result.status == "error"
        if arm == "builtin":
            assert result.initial.answered and result.initial.search_completed == 1
        assert not result.initial.response_complete and not result.followup.started
    assert len(captured) == 1
    assert stream.closed


@pytest.mark.parametrize("arm", ["client", "builtin"])
async def test_outer_cancellation_closes_active_stream_and_sends_no_followup(arm):
    stream = GatedStream()
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, stream=stream)

    task = asyncio.create_task(probe.compare(
        "gpt-test", "test", arm, transport=httpx.MockTransport(respond),
    ))
    await asyncio.wait_for(stream.waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and len(captured) == 1


@pytest.mark.parametrize("stage", ["response", "search", "followup"])
@pytest.mark.parametrize("failure,category", [
    (httpx.Response(401, json={"error": {"message": "private-auth"}}), "authentication"),
    (httpx.Response(429, json={"error": {"message": "private-limit"}}), "rate_limit"),
    (httpx.Response(400, json={"error": {"code": "quota_exceeded"}}), "rate_limit"),
])
async def test_auth_and_quota_stop_remaining_trials_without_retry(stage, failure, category):
    replies = []
    if stage in {"search", "followup"}:
        replies.append(sse(item(search_call()), COMPLETED))
    if stage == "followup":
        replies.extend([httpx.Response(200, json={"output": "private-result"}), sse(*answer())])
    replies.append(failure)
    calls = []

    def respond(request):
        calls.append(request)
        return replies.pop(0)

    report, code = await probe.compare("gpt-test", "test", transport=httpx.MockTransport(respond))
    assert code == 2 and report["status"] == "blocked"
    assert report["trials"][0]["error"] == category
    assert all(t["status"] == "not_run" for t in report["trials"][1:])
    assert len(calls) == (1 if stage == "response" else 2 if stage == "search" else 4)
    assert not replies
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize("first_arm", ["client", "builtin"])
@pytest.mark.parametrize("error_code,category", [
    ("token_invalidated", "authentication"), ("quota_exceeded", "rate_limit"),
])
async def test_streamed_auth_and_quota_stop_both_arms(first_arm, error_code, category):
    calls = []

    def respond(request):
        calls.append(request)
        return sse({"type": "response.failed", "response": {
            "error": {"code": error_code, "message": "private-error-message"},
        }})

    report, code = await probe.compare(
        "gpt-test", "test", first_arm, transport=httpx.MockTransport(respond),
    )
    assert code == 2 and report["trials"][0]["error"] == category
    assert all(t["status"] == "not_run" for t in report["trials"][1:])
    assert len(calls) == 1
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize("failure,category", [
    (httpx.ConnectError("private-host-token"), "http"),
    (httpx.ReadTimeout("private-host-token"), "timeout"),
    (RuntimeError("private-backend-token"), "runtime"),
    (ValueError("private-unknown-token"), "unexpected"),
])
async def test_typed_errors_retain_attempt_counts_and_emit_closed_categories(failure, category):
    result, _ = await trial("client", [sse(item(search_call()), COMPLETED), failure])
    assert result.error == category and result.status == "error"
    assert result.initial.http_requests == 2 and result.initial.search_requests == 1
    assert result.initial.search_calls == 1 and result.initial.search_completed == 0
    assert not result.followup.started
    assert "private-" not in json.dumps(probe.asdict(result))


@pytest.mark.parametrize("first_arm", ["client", "builtin"])
@pytest.mark.parametrize("search_rounds,request_count", [(1, 18), (4, 36)])
async def test_bounded_order_and_full_run_gates(first_arm, search_rounds, request_count):
    captured = []

    def respond(request):
        payload = json.loads(request.content)
        captured.append((request.url.path, payload))
        if request.url.path.endswith("/alpha/search"):
            return httpx.Response(200, json={"output": "private-result"})
        tools = payload.get("tools", [])
        if len(payload["input"]) == 1:
            assert "cookie" not in request.headers, "Each arm needs a fresh HTTP client"
        else:
            assert request.headers["cookie"] == "probe=private-cookie"
        previous_calls = sum(i.get("type") == "function_call" for i in payload["input"])
        if tools and tools[0]["type"] == "function" and previous_calls < search_rounds:
            response = sse(item(search_call(previous_calls, arguments={
                "queries": [], "open_urls": [probe.DIRECT_URL],
            })), COMPLETED)
        elif tools and tools[0]["type"] == "web_search":
            response = sse(item(builtin_search(action={
                "type": "open_page", "url": probe.DIRECT_URL,
            })), *answer())
        else:
            response = sse(*answer())
        response.headers["set-cookie"] = "probe=private-cookie; Path=/"
        return response

    report, code = await probe.compare(
        "gpt-test", "test", first_arm, transport=httpx.MockTransport(respond),
    )
    assert code == 0 and report["status"] == "completed"
    expected = ["client", "builtin", "builtin", "client", "client", "builtin"]
    if first_arm == "builtin":
        expected = ["builtin" if arm == "client" else "client" for arm in expected]
    assert [t["arm"] for t in report["trials"]] == expected
    assert [t["case"] for t in report["trials"]] == [
        "iana_maintainer", "iana_maintainer", "python_stable", "python_stable",
        "iana_direct", "iana_direct",
    ]
    assert len(captured) == request_count
    assert sum(t["initial"]["started"] + t["followup"]["started"] for t in report["trials"]) == 12
    assert all(t["gate_pass"] for t in report["trials"])
    for result in report["trials"]:
        assert result["initial"]["http_requests"] + result["followup"]["http_requests"] == (
            2 * search_rounds + 2 if result["arm"] == "client" else 2
        )
    assert report["live_request_sent"]
    assert "private-" not in json.dumps(report)


async def test_evaluated_failure_still_runs_other_trials_and_exits_one():
    requests = []

    def respond(request):
        requests.append(request)
        return sse(*answer())

    report, code = await probe.compare("gpt-test", "test", transport=httpx.MockTransport(respond))
    assert code == 1 and report["status"] == "failed"
    assert len(requests) == 6
    assert all(t["status"] == "search_not_established" for t in report["trials"])


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_dry_run_and_missing_credentials_never_create_client(monkeypatch, capsys, dry_run):
    def forbidden(*args, **kwargs):
        pytest.fail("No HTTP client may be constructed")

    monkeypatch.setattr(probe.httpx, "AsyncClient", forbidden)
    monkeypatch.delenv("CODEX_ASSIST_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["probe", "--model", "gpt-test"] + (
        ["--dry-run"] if dry_run else []
    ))
    assert probe.main() == (0 if dry_run else 2)
    report = json.loads(capsys.readouterr().out)
    assert report["live_request_sent"] is False
    if dry_run:
        assert report["max_arms"] == 6 and report["max_turns"] == 12
        assert report["max_http_requests"] == 36
        assert report["turn_seconds"] == 60 and report["arm_seconds"] == 120
        assert report["total_seconds"] == 720
    else:
        assert report["error"] == "missing_credentials"


@pytest.mark.parametrize("extra", [
    [], ["--model", "https://private-host/token"], ["--model", "private model text"],
    ["--model", "gpt-test", "--pairs", "2"], ["--model", "gpt-test", "--token", "private-token"],
])
def test_cli_rejects_missing_model_or_unsupported_arguments(extra, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["probe", *extra])
    with pytest.raises(SystemExit) as exc:
        probe.main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "status": "blocked", "error": "invalid_arguments", "live_request_sent": False,
    }
    assert not output.err


def test_cli_prints_partial_sanitized_json_on_overall_timeout(monkeypatch, capsys):
    stream = GatedStream()
    actual_client = httpx.AsyncClient

    def offline_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream),
        )
        return actual_client(**kwargs)

    monkeypatch.setattr(probe.httpx, "AsyncClient", offline_client)
    monkeypatch.setattr(probe, "TOTAL_SECONDS", 0.01)
    monkeypatch.setenv("CODEX_ASSIST_ACCESS_TOKEN", "private-synthetic-token")
    monkeypatch.setattr("sys.argv", ["probe", "--model", "gpt-test", "--first-arm", "builtin"])
    assert probe.main() == 3
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "incomplete" and report["live_request_sent"]
    assert report["trials"][0]["initial"]["answered"]
    assert not report["trials"][0]["initial"]["response_complete"]
    assert all(t["status"] == "not_run" for t in report["trials"][1:])
    assert "private-" not in output.out and not output.err
    assert stream.closed
