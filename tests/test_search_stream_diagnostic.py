from __future__ import annotations

import asyncio
import json
import runpy
import subprocess
import sys
from contextlib import aclosing
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from custom_components.codex_assist import codex_client
from scripts import probe_search_comparison as probe
from scripts import search_stream_diagnostic as diagnostic

PRIVATE = "private-redaction-sentinel"
COMPLETED = {"type": "response.completed"}
REASONING = {"type": "reasoning", "id": PRIVATE, "encrypted_content": PRIVATE}
SEARCH = {
    "type": "web_search_call", "id": PRIVATE, "status": "completed",
    "action": {"type": "search", "query": PRIVATE},
}
MESSAGE = {"type": "message", "role": "assistant", "content": [
    {"type": "output_text", "text": PRIVATE},
]}


def encode(*events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def item(value):
    return {"type": "response.output_item.done", "item": value}


def success(*native):
    return httpx.Response(200, content=encode(
        *(item(value) for value in native),
        {"type": "response.output_text.delta", "delta": PRIVATE}, COMPLETED,
    ))


async def run(replies):
    requests = []
    responses = []

    def respond(request):
        requests.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        responses.append(reply)
        return reply

    original = codex_client._aiter_sse_events
    report, code = await diagnostic.diagnose(
        "gpt-test", PRIVATE, transport=httpx.MockTransport(respond),
    )
    assert codex_client._aiter_sse_events is original
    assert all(response.is_closed for response in responses)
    assert PRIVATE not in json.dumps(report)
    return report, code, requests


@pytest.mark.parametrize("ending,expected", [
    ({"type": "error", "message": PRIVATE}, "event_error"),
    ({"type": "response.failed", "response": {"error": {"message": PRIVATE}}}, "event_failed"),
    ({"type": "response.incomplete", "response": {
        "incomplete_details": {"reason": PRIVATE},
    }}, "event_incomplete"),
])
async def test_terminal_events_are_seen_before_the_real_client_raises(ending, expected):
    report, code, requests = await run([
        success(REASONING, SEARCH, MESSAGE), httpx.Response(200, content=encode(ending)),
    ])
    trace = report["followup"]
    assert code == 3 and report["status"] == "incomplete"
    assert report["initial"]["outcome"] == "completed"
    assert trace["outcome"] == trace["terminal_kind"] == expected
    assert trace["stage"] == "events" and not trace["natural_eof"]
    assert trace["events"][ending["type"]] == 1
    assert len(requests) == 2


@pytest.mark.parametrize("body,expected,event_count", [
    (b"", "eof_empty", 0),
    (b"data: not-json-private-redaction-sentinel\n\n", "eof_undecoded", 0),
    (b"data: [DONE]\n\n", "eof_undecoded", 0),
    (encode({"type": "response.created"}), "eof_after_events", 1),
])
async def test_natural_eof_distinguishes_empty_undecoded_and_events(body, expected, event_count):
    report, code, requests = await run([httpx.Response(200, content=body)])
    trace = report["initial"]
    assert code == 3 and trace["outcome"] == expected
    assert trace["http_200"] and trace["header_received"] and trace["status_class"] == "2xx"
    assert trace["natural_eof"] and trace["terminal_kind"] == "none"
    assert sum(trace["events"].values()) == event_count
    assert (trace["decoded_lines"] == 0) is (body == b"")
    assert report["followup"]["outcome"] == "not_run" and len(requests) == 1


@pytest.mark.parametrize("ending,expected", [
    ("error", "event_error"),
    ("response.failed", "event_failed"),
    ("response.incomplete", "event_incomplete"),
    ("response.completed", "completed"),
])
async def test_terminal_event_flushed_at_line_eof_preserves_eof_evidence(ending, expected):
    event = {"type": ending, "code": "invalid_value", "param": "input"}
    body = f"data: {json.dumps(event)}".encode()
    report, code, requests = await run([httpx.Response(200, content=body)])
    trace = report["initial"]
    assert trace["terminal_kind"] == expected
    assert trace["outcome"] == expected
    assert trace["natural_eof"] is True
    assert len(requests) == 1
    assert code == (1 if ending == "response.completed" else 3)
    assert trace["structured_errors"] == {
        "error": [{
            "source": "event", "code": "invalid_value", "type": "missing", "param": "input",
        }],
        "response.failed": [{
            "source": "response_error", "code": "missing", "type": "missing", "param": "missing",
        }],
        "response.incomplete": None, "response.completed": None,
    }[ending]


@pytest.mark.parametrize("failure,expected,code", [
    (httpx.StreamConsumed(), "stream_error", 3),
    (httpx.ConnectError(PRIVATE), "transport_error", 1),
    (httpx.ReadTimeout(PRIVATE), "timeout", 1),
    (diagnostic.ProbeObserverError(PRIVATE), "probe_error", 3),
    (diagnostic.ProbeBudgetError(PRIVATE), "probe_error", 3),
    (RuntimeError("Codex request failed with status private-redaction-sentinel"),
     "unattributed_runtime", 3),
    (ValueError(PRIVATE), "unexpected", 3),
    (codex_client.CodexAuthenticationError(PRIVATE), "authentication", 2),
    (codex_client.CodexRateLimitError(PRIVATE), "rate_limit", 2),
])
async def test_typed_errors_take_precedence_without_reading_messages(failure, expected, code):
    report, actual_code, requests = await run([failure])
    assert actual_code == code and report["initial"]["outcome"] == expected
    assert report["initial"]["stage"] == "request"
    assert not report["initial"]["natural_eof"] and len(requests) == 1


@pytest.mark.parametrize("status,body,expected,code", [
    (201, {"error": {"message": PRIVATE}}, "http_status", 3),
    (500, {"error": {"message": PRIVATE}}, "http_status", 3),
    (401, {"error": {"message": PRIVATE}}, "authentication", 2),
    (429, {"error": {"message": PRIVATE}}, "rate_limit", 2),
    (400, {"error": {"code": "quota_exceeded", "message": PRIVATE}}, "rate_limit", 2),
])
async def test_http_status_and_typed_auth_quota_do_not_retry(status, body, expected, code):
    report, actual_code, requests = await run([httpx.Response(status, json=body)])
    trace = report["initial"]
    assert actual_code == code and trace["outcome"] == expected
    assert trace["header_received"] and not trace["http_200"]
    assert trace["stage"] == "headers" and sum(trace["events"].values()) == 0
    assert not trace["natural_eof"] and len(requests) == 1
    assert "quota_exceeded" not in json.dumps(report)


@pytest.mark.parametrize("followup", [False, True])
@pytest.mark.parametrize("error_code,expected", [
    ("token_invalidated", "authentication"), ("rate_limit_exceeded", "rate_limit"),
    ("usage_limit_reached", "rate_limit"), ("usage_not_included", "rate_limit"),
    ("quota_exceeded", "rate_limit"),
])
async def test_typed_stream_errors_keep_terminal_evidence(error_code, expected, followup):
    replies = [success(REASONING, SEARCH, MESSAGE)] if followup else []
    replies.append(httpx.Response(200, content=encode({
        "type": "response.failed", "response": {"error": {
            "code": error_code, "message": PRIVATE,
        }},
    })))
    report, code, requests = await run(replies)
    trace = report["followup" if followup else "initial"]
    assert code == 2 and trace["outcome"] == expected
    assert trace["terminal_kind"] == "event_failed" and not trace["natural_eof"]
    assert len(requests) == (2 if followup else 1)
    assert trace["structured_errors"] == [{
        "source": "response_error", "code": error_code, "type": "missing", "param": "missing",
    }]


@pytest.mark.parametrize("followup", [False, True])
@pytest.mark.parametrize("event,identities,outcome,exit_code", [
    ({"type": "error", "code": "invalid_value", "param": "input"}, [
        {"source": "event", "code": "invalid_value", "type": "missing", "param": "input"},
    ], "event_error", 3),
    ({"type": "error", "error": {
        "code": "invalid_request", "type": "invalid_request_error", "param": "include",
    }}, [
        {"source": "event", "code": "missing", "type": "missing", "param": "missing"},
        {"source": "event_error", "code": "invalid_request",
         "type": "invalid_request_error", "param": "include"},
    ], "event_error", 3),
    ({"type": "response.failed", "response": {"error": {
        "code": "item_not_found", "type": "invalid_request_error", "param": "previous_response_id",
    }}}, [
        {"source": "response_error", "code": "item_not_found",
         "type": "invalid_request_error", "param": "previous_response_id"},
    ], "event_failed", 3),
    ({"type": "error", "code": "token_invalidated", "param": "model", "error": {
        "code": "server_error", "type": "internal_server_error", "param": "tools",
    }}, [
        {"source": "event", "code": "token_invalidated", "type": "missing", "param": "model"},
        {"source": "event_error", "code": "server_error",
         "type": "internal_server_error", "param": "tools"},
    ], "event_error", 3),
    ({"type": "error", "param": "input[0].encrypted_content"}, [
        {"source": "event", "code": "missing", "type": "missing", "param": "other"},
    ], "event_error", 3),
    ({"type": "error", "error": {"type": "authentication_error"}}, [
        {"source": "event", "code": "missing", "type": "missing", "param": "missing"},
        {"source": "event_error", "code": "missing",
         "type": "authentication_error", "param": "missing"},
    ], "event_error", 3),
    ({"type": "response.failed", "response": {"error": {"code": "token_invalidated"}}}, [
        {"source": "response_error", "code": "token_invalidated",
         "type": "missing", "param": "missing"},
    ], "authentication", 2),
    ({"type": "response.failed", "response": {"error": {"code": "quota_exceeded"}}}, [
        {"source": "response_error", "code": "quota_exceeded",
         "type": "missing", "param": "missing"},
    ], "rate_limit", 2),
])
async def test_structured_error_sources_are_independent_of_outcome(
    followup, event, identities, outcome, exit_code,
):
    replies = [success(REASONING, SEARCH, MESSAGE)] if followup else []
    replies.append(httpx.Response(200, content=encode(event)))
    report, code, requests = await run(replies)
    trace = report["followup" if followup else "initial"]
    assert json.loads(json.dumps(trace["structured_errors"])) == identities
    assert trace["outcome"] == outcome and code == exit_code
    assert len(requests) == report["http_requests"] == (2 if followup else 1)
    assert trace["events"][event["type"]] == 1 and trace["http_200"] is True
    assert trace["natural_eof"] is False
    assert report["initial" if followup else "followup"]["structured_errors"] is None


@pytest.mark.parametrize("followup", [False, True])
@pytest.mark.parametrize("label", [
    "token_invalidated", "invalid_api_key", "authentication_error",
    "rate_limit_exceeded", "usage_limit_reached", "usage_not_included", "quota_exceeded",
    "invalid_request_error", "invalid_request", "invalid_prompt", "invalid_value",
    "missing_required_parameter", "unsupported_parameter", "item_not_found",
    "server_error", "internal_server_error",
])
async def test_exact_identity_labels_are_reported_without_changing_top_level_exit(label, followup):
    event = {"type": "error", "code": label, "error": {"code": label, "type": label}}
    replies = [success(REASONING, SEARCH, MESSAGE)] if followup else []
    replies.append(httpx.Response(200, content=encode(event)))
    report, code, _ = await run(replies)
    assert code == 3
    assert report["followup" if followup else "initial"]["structured_errors"] == [
        {"source": "event", "code": label, "type": "missing", "param": "missing"},
        {"source": "event_error", "code": label, "type": label, "param": "missing"},
    ]


@pytest.mark.parametrize("param", [
    "input", "tools", "model", "store", "previous_response_id", "include", "reasoning",
])
async def test_exact_param_labels_are_reported(param):
    report, code, _ = await run([httpx.Response(200, content=encode({
        "type": "response.failed", "response": {"error": {"param": param}},
    }))])
    assert code == 3 and report["initial"]["structured_errors"] == [{
        "source": "response_error", "code": "missing", "type": "missing", "param": param,
    }]


@pytest.mark.parametrize("followup", [False, True])
@pytest.mark.parametrize("value", [
    None, [], {PRIVATE: PRIVATE}, True, 42, "", "INVALID_REQUEST", PRIVATE * 1000,
    "https://example.invalid/private-redaction-sentinel", "input[0].encrypted_content",
])
async def test_hostile_error_fields_and_keys_never_escape(followup, value):
    error = {
        "code": value, "type": value, "param": value,
        "message": PRIVATE * 1000, "detail": {PRIVATE: PRIVATE},
        "payload": PRIVATE, "traceback": PRIVATE, PRIVATE: PRIVATE,
    }
    events = [
        {**error, "type": "error", "error": error},
        {"type": "response.failed", "response": {"error": error, PRIVATE: PRIVATE},
         "error": error, "code": "token_invalidated", "param": "input", PRIVATE: PRIVATE},
    ]
    for event, expected in zip(events, [
        [{"source": "event", "code": "other", "type": "missing", "param": "other"},
         {"source": "event_error", "code": "other", "type": "other", "param": "other"}],
        [{"source": "response_error", "code": "other", "type": "other", "param": "other"}],
    ], strict=True):
        replies = [success(REASONING, SEARCH, MESSAGE)] if followup else []
        replies.append(httpx.Response(200, content=encode(event)))
        report, code, _ = await run(replies)
        assert code == 3
        assert report["followup" if followup else "initial"]["structured_errors"] == expected


@pytest.mark.parametrize("value", [None, [], PRIVATE * 1000, True, 7, {}])
@pytest.mark.parametrize("followup", [False, True])
async def test_missing_or_malformed_error_containers_have_no_identity_fields(value, followup):
    for event, sources in [
        ({"type": "error", "error": value}, ["event", "event_error"]),
        ({"type": "response.failed", "response": {"error": value}}, ["response_error"]),
        ({"type": "response.failed", "response": value}, ["response_error"]),
        ({"type": "response.failed"}, ["response_error"]),
    ]:
        replies = [success(REASONING, SEARCH, MESSAGE)] if followup else []
        replies.append(httpx.Response(200, content=encode(event)))
        report, code, _ = await run(replies)
        assert code == 3
        assert report["followup" if followup else "initial"]["structured_errors"] == [
            {"source": source, "code": "missing", "type": "missing", "param": "missing"}
            for source in sources
        ]


async def test_success_ignores_error_looking_fields_and_new_invocation_does_not_inherit_errors():
    report, code, _ = await run([httpx.Response(200, content=encode({
        "type": "error", "code": "invalid_prompt", "param": "input",
    }))])
    assert code == 3 and report["initial"]["structured_errors"] == [{
        "source": "event", "code": "invalid_prompt", "type": "missing", "param": "input",
    }]
    untrusted = {
        "error": {"code": "token_invalidated", "type": "authentication_error", "param": "input"},
        "code": PRIVATE, "param": PRIVATE, "response": {"error": {"code": "quota_exceeded"}},
        PRIVATE: PRIVATE,
    }
    report, code, _ = await run([
        httpx.Response(200, content=encode(
            item(SEARCH), item(MESSAGE), {"type": "response.output_text.delta", "delta": PRIVATE},
            {**COMPLETED, **untrusted},
        )),
        success(MESSAGE),
    ])
    assert code == 0 and report["gate_pass"] is True
    assert report["initial"]["structured_errors"] is None
    assert report["followup"]["structured_errors"] is None


@pytest.mark.parametrize("encrypted", [True, False])
async def test_replay_shape_comes_from_unmodified_actual_outgoing_history(encrypted):
    reasoning = dict(REASONING)
    if not encrypted:
        reasoning.pop("encrypted_content")
    report, code, requests = await run([
        success(reasoning, SEARCH, MESSAGE), success(MESSAGE),
    ])
    initial, followup = [json.loads(request.content) for request in requests]
    assert code == 0 and report["gate_pass"] and report["http_requests"] == 2
    assert initial["input"] == [{"role": "user", "content": probe.CASES[0].prompt}]
    assert initial["tools"] == [probe.SEARCH_TOOL]
    assert followup["input"] == [
        *initial["input"], reasoning, SEARCH, MESSAGE,
        {"role": "user", "content": probe.FOLLOWUP},
    ]
    assert "tools" not in followup
    shape = report["followup"]["replay"]
    assert shape["ordered_prefix"] == ["other", "reasoning", "web_search_call", "message", "other"]
    assert shape["item_types"] == {
        "message": 1, "reasoning": 1, "web_search_call": 1,
        "function_call": 0, "function_call_output": 0, "other": 2,
    }
    assert shape["field_presence"] == {"id": 2, "status": 1, "action": 1}
    assert shape["reasoning_encrypted_present"] == int(encrypted)
    assert shape["reasoning_encrypted_nonempty"] == int(encrypted)
    assert "include" not in followup and shape["include_encrypted_reasoning"] is False
    assert shape["tools_count"] == 0 and shape["store"] is False
    assert all(report[turn]["terminal_kind"] == "completed" for turn in ("initial", "followup"))
    assert not any(report[turn]["natural_eof"] for turn in ("initial", "followup"))


def test_replay_shape_bounds_order_and_buckets_hostile_values():
    payload = {"include": ["reasoning.encrypted_content"], "store": True, "input": [
        {"type": []}, {"type": {}}, {"type": PRIVATE * 1000, PRIVATE: PRIVATE},
        {"type": "reasoning", "encrypted_content": []},
        {"type": "reasoning", "encrypted_content": ""},
        {"type": "reasoning", "encrypted_content": PRIVATE},
        *[{"type": "message"}] * 34,
    ]}
    shape = diagnostic.replay_shape(payload)
    assert shape["ordered_prefix"] == ["other"] * 3 + ["reasoning"] * 3 + ["message"] * 26
    assert shape["omitted_items"] == 8 and shape["item_types"]["message"] == 34
    assert shape["reasoning_encrypted_present"] == 3
    assert shape["reasoning_encrypted_nonempty"] == 1
    assert shape["include_encrypted_reasoning"] is True and shape["store"] is True
    assert PRIVATE not in json.dumps(shape)


@pytest.mark.parametrize("kind", [[], {}, PRIVATE * 1000])
async def test_hostile_event_types_are_safely_bucketed(kind):
    report, code, _ = await run([httpx.Response(200, content=encode({"type": kind}))])
    assert code == 3 and report["initial"]["events"]["other"] == 1
    assert report["initial"]["outcome"] in {"unexpected", "eof_after_events"}


@pytest.mark.parametrize("native,gate", [
    ([], "initial_answer_not_established"),
    ([MESSAGE], "search_not_established"),
    ([dict(SEARCH, status="failed"), MESSAGE], "search_not_established"),
    ([SEARCH], "completed"),
    ([SEARCH, {"type": "function_call", "name": PRIVATE, "arguments": "{}"}], "unexpected_tool"),
])
async def test_existing_initial_gates_control_followup(native, gate):
    replies = [success(*native)]
    if gate == "completed":
        replies.append(success(MESSAGE))
    report, code, requests = await run(replies)
    assert report["gate"] == gate
    assert code == (0 if gate == "completed" else 1)
    assert len(requests) == (2 if gate == "completed" else 1)


@pytest.mark.parametrize("followup", [False, True])
async def test_empty_answer_fails_gate_without_claiming_stream_failure(followup):
    empty = httpx.Response(200, content=encode(item(SEARCH), item(MESSAGE), COMPLETED))
    replies = [success(SEARCH, MESSAGE), httpx.Response(200, content=encode(COMPLETED))] if (
        followup
    ) else [empty]
    report, code, requests = await run(replies)
    assert code == 1 and report["gate"] == (
        "followup_answer_not_established" if followup else "initial_answer_not_established"
    )
    assert report["followup" if followup else "initial"]["outcome"] == "completed"
    assert len(requests) == (2 if followup else 1)


async def test_followup_search_is_an_evaluated_failure():
    report, code, requests = await run([success(SEARCH, MESSAGE), success(SEARCH, MESSAGE)])
    assert code == 1 and report["gate"] == "unexpected_tool"
    assert report["followup"]["outcome"] == "completed" and len(requests) == 2


async def test_request_hook_blocks_third_send_before_transport():
    calls = []
    async with diagnostic.StreamObserver() as observer, httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request) or success(MESSAGE)),
        event_hooks={"request": [observer.request]},
    ) as http:
        for _ in range(2):
            await http.post("https://example.invalid/responses", json={"input": []})
        with pytest.raises(diagnostic.ProbeBudgetError):
            await http.post("https://example.invalid/responses", json={"input": []})
    assert len(calls) == observer.requests == 2


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, *, complete=False, failure=None, search=True):
        self.complete = complete
        self.failure = failure
        self.search = search
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        yield encode(
            *([item(SEARCH)] if self.search else []), item(MESSAGE),
            {"type": "response.output_text.delta", "delta": PRIVATE},
        )
        if self.failure:
            raise self.failure
        if self.complete:
            yield encode(COMPLETED)
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("deadline", ["turn", "overall", "cancel"])
async def test_cleanup_and_restoration_on_deadline_or_cancel(deadline, monkeypatch):
    stream = ControlledStream()
    original = codex_client._aiter_sse_events
    response = httpx.Response(200, stream=stream)
    original_lines = response.aiter_lines
    line_closed = False

    async def lines():
        nonlocal line_closed
        try:
            async with aclosing(original_lines()) as decoded:
                async for line in decoded:
                    yield line
        finally:
            line_closed = True

    response.aiter_lines = lines
    monkeypatch.setattr(probe, "TURN_SECONDS", 0.01 if deadline == "turn" else 10)
    monkeypatch.setattr(probe, "ARM_SECONDS", 0.01 if deadline == "overall" else 20)
    task = asyncio.create_task(diagnostic.diagnose(
        "gpt-test", PRIVATE, transport=httpx.MockTransport(
            lambda request: response,
        ),
    ))
    if deadline == "cancel":
        await asyncio.wait_for(stream.waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        report, code = await task
        assert code == (1 if deadline == "turn" else 3)
        assert report["initial"]["outcome"] == "timeout"
        assert not report["initial"]["natural_eof"]
        assert report["http_requests"] == 1
        assert PRIVATE not in json.dumps(report)
    assert stream.closed and line_closed and codex_client._aiter_sse_events is original
    report, code, _ = await run([success(SEARCH, MESSAGE), success(MESSAGE)])
    assert code == 0 and report["followup"]["outcome"] == "completed"


@pytest.mark.parametrize("failure", [None, httpx.StreamConsumed(), RuntimeError(PRIVATE)])
async def test_owned_iterators_close_without_waiting_for_eof(failure, monkeypatch):
    original = codex_client._aiter_sse_events
    closed = []

    async def tracked(response):
        try:
            async with aclosing(original(response)) as events:
                async for event in events:
                    yield event
        finally:
            closed.append(response)

    monkeypatch.setattr(codex_client, "_aiter_sse_events", tracked)
    streams = [ControlledStream(complete=True, failure=failure)]
    replies = [httpx.Response(200, stream=streams[0], headers={"x-private-sentinel": PRIVATE})]
    if failure is None:
        streams.append(ControlledStream(complete=True, search=False))
        replies.append(httpx.Response(200, stream=streams[1]))
    report, code, requests = await asyncio.wait_for(run(replies), 1)
    assert code == (0 if failure is None else 3)
    assert len(closed) == len(requests) and all(stream.closed for stream in streams)
    assert not report["initial"]["natural_eof"]
    if failure:
        assert report["initial"]["outcome"] == (
            "stream_error" if isinstance(failure, httpx.StreamError) else "unattributed_runtime"
        )
    else:
        assert not any(stream.waiting.is_set() for stream in streams)


async def test_response_close_error_does_not_turn_terminal_completion_into_success():
    class CloseFailure(ControlledStream):
        async def aclose(self):
            self.closed = True
            raise RuntimeError(PRIVATE)

    stream = CloseFailure(complete=True)
    report, code, requests = await run([httpx.Response(200, stream=stream)])
    assert code == 3 and report["gate"] == "error"
    assert report["initial"]["terminal_kind"] == "completed"
    assert report["initial"]["outcome"] == "unattributed_runtime"
    assert not report["initial"]["natural_eof"] and len(requests) == 1 and stream.closed


async def test_overlap_rejected_before_client_creation_or_request(monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("Overlapping diagnostic must not construct another client")

    async with diagnostic.StreamObserver() as outer:
        patched = codex_client._aiter_sse_events
        monkeypatch.setattr(diagnostic.httpx, "AsyncClient", forbidden)
        report, code = await diagnostic.diagnose("gpt-test", PRIVATE)
        assert code == 3 and report["initial"]["outcome"] == "probe_error"
        assert report["http_requests"] == outer.requests == 0
        assert codex_client._aiter_sse_events is patched


async def test_unrelated_response_passes_through_without_contamination():
    async with diagnostic.StreamObserver() as observer:
        response = success(MESSAGE)
        events = [event async for event in codex_client._aiter_sse_events(response)]
        assert events[-1] == COMPLETED
        assert observer.trace == diagnostic.StreamTrace()
        assert observer._responses == {} and observer._iterators == []


@pytest.mark.parametrize("kind", ["response.completed", "error", "response.failed"])
async def test_observer_yields_identical_objects_without_reading_freeform_fields(kind, monkeypatch):
    class StructuredOnly(dict):
        def get(self, key, default=None):
            assert key in {"type", "code", "param", "error", "response"}
            return super().get(key, default)

        def __iter__(self):
            pytest.fail("The observer must not enumerate event fields")

    class PrivateValue:
        def __str__(self):
            pytest.fail("The observer must not stringify unknown values")

    error = StructuredOnly(
        code="invalid_value", type="invalid_request_error", param=PrivateValue(),
        message=PRIVATE, detail=PRIVATE, payload=PRIVATE, traceback=PRIVATE,
    )
    event = StructuredOnly(
        type=kind, code="server_error", param="tools", error=error,
        response=StructuredOnly(error=error), message=PRIVATE, detail=PRIVATE,
    )
    closed = False

    async def original(response):
        nonlocal closed
        try:
            yield event
            await asyncio.Event().wait()
        finally:
            closed = True

    monkeypatch.setattr(codex_client, "_aiter_sse_events", original)
    response = httpx.Response(200)
    async with diagnostic.StreamObserver() as observer:
        await observer.response(response)
        iterator = codex_client._aiter_sse_events(response)
        assert await anext(iterator) is event
        await observer.close_turn()
        assert closed and not observer.trace.natural_eof
        assert observer.trace.terminal_kind == {
            "response.completed": "completed", "error": "event_error",
            "response.failed": "event_failed",
        }[kind]
        assert asdict(observer.trace)["structured_errors"] == {
            "response.completed": None,
            "error": [
                {"source": "event", "code": "server_error", "type": "missing", "param": "tools"},
                {"source": "event_error", "code": "invalid_value",
                 "type": "invalid_request_error", "param": "other"},
            ],
            "response.failed": [
                {"source": "response_error", "code": "invalid_value",
                 "type": "invalid_request_error", "param": "other"},
            ],
        }[kind]
        assert PRIVATE not in json.dumps(asdict(observer.trace))
    assert codex_client._aiter_sse_events is original


@pytest.mark.parametrize("failure", [False, True])
async def test_observed_and_unobserved_real_client_requests_deltas_and_exceptions_match(failure):
    captured = []

    def respond(request):
        captured.append(request.content)
        if failure:
            return httpx.Response(200, content=encode(
                {"type": "response.output_text.delta", "delta": PRIVATE},
                {"type": "error", "message": PRIVATE},
            ))
        return success(REASONING, SEARCH, MESSAGE)

    async def consume(http):
        deltas = []
        error = None
        client = codex_client.CodexClient(http_client=http, access_token=PRIVATE)
        try:
            async with aclosing(client._stream_once(
                model="gpt-test", instructions=probe.INSTRUCTIONS,
                input_items=[{"role": "user", "content": probe.CASES[0].prompt}],
                tools=[probe.SEARCH_TOOL],
            )) as stream:
                async for delta in stream:
                    deltas.append(delta)
        except Exception as exc:
            error = type(exc)
        return deltas, error

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        plain = await consume(http)
    async with diagnostic.StreamObserver() as observer, httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        event_hooks={"request": [observer.request], "response": [observer.response]},
    ) as http:
        observed = await consume(http)
    assert captured[0] == captured[1] and observed == plain
    assert plain[1] is (RuntimeError if failure else None)
    if failure:
        assert plain[0] == [codex_client.CodexTextDelta(PRIVATE)]
    else:
        assert plain[0][0] == codex_client.CodexResponseItemDelta(REASONING)


async def test_default_comparison_never_installs_observer():
    original = codex_client._aiter_sse_events
    calls = []

    def respond(request):
        assert codex_client._aiter_sse_events is original
        calls.append(request)
        return success(MESSAGE)

    report, code = await probe.compare("gpt-test", PRIVATE, transport=httpx.MockTransport(respond))
    assert code == 1 and len(calls) == 6
    assert all(trial["status"] == "search_not_established" for trial in report["trials"])
    assert "mode" not in report and codex_client._aiter_sse_events is original
    assert PRIVATE not in json.dumps(report)


def test_cli_main_failed_followup_is_sanitized_json_exit_three(monkeypatch, capsys):
    actual_client = httpx.AsyncClient
    replies = [success(REASONING, SEARCH, MESSAGE), httpx.Response(200, content=encode({
        "type": "error", "code": "invalid_value", "param": "input", "message": PRIVATE,
        "error": {"code": PRIVATE, "type": "invalid_request_error", "param": PRIVATE,
                  "detail": PRIVATE, PRIVATE: PRIVATE},
    }))]

    def offline_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: replies.pop(0))
        return actual_client(**kwargs)

    monkeypatch.setattr(probe.httpx, "AsyncClient", offline_client)
    monkeypatch.setenv("CODEX_ASSIST_ACCESS_TOKEN", PRIVATE)
    monkeypatch.setattr(sys, "argv", ["probe", "--model", "gpt-test", "--builtin-followup-only"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(Path(probe.__file__)), run_name="__main__")
    assert stopped.value.code == 3
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["followup"]["outcome"] == "event_error" and report["http_requests"] == 2
    assert report["followup"]["structured_errors"] == [
        {"source": "event", "code": "invalid_value", "type": "missing", "param": "input"},
        {"source": "event_error", "code": "other",
         "type": "invalid_request_error", "param": "other"},
    ]
    assert report["initial"]["outcome"] == "completed"
    assert report["initial"]["structured_errors"] is None
    assert not replies and PRIVATE not in output.out and not output.err


@pytest.mark.parametrize("flags,code", [
    (["--builtin-followup-only", "--dry-run"], 0),
    (["--builtin-followup-only"], 2),
    (["--builtin-followup-only", "--first-arm", "client", "--dry-run"], 2),
    (["--builtin-followup-only", "--first-arm", "builtin", "--dry-run"], 2),
])
def test_real_cli_subprocess_bounds_missing_credentials_and_flag_rejection(flags, code):
    child = (
        "import os, runpy, sys\n"
        "if 'CODEX_ASSIST_ACCESS_TOKEN' in os.environ:\n"
        "    del os.environ['CODEX_ASSIST_ACCESS_TOKEN']\n"
        "sys.argv = ['scripts/probe_search_comparison.py', *sys.argv[1:]]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", child, "--model", "gpt-test", *flags],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == code and not result.stderr
    report = json.loads(result.stdout)
    assert report["live_request_sent"] is False
    if code == 0:
        assert report == {
            "mode": "builtin_followup_only", "model": "gpt-test", "status": "dry_run",
            "cases": ["iana_maintainer"], "live_request_sent": False,
            "max_arms": 1, "max_turns": 2, "max_http_requests": 2,
            "turn_seconds": 60, "total_seconds": 120,
        }
    else:
        assert report["error"] == ("invalid_arguments" if "--first-arm" in flags else
                                   "missing_credentials")
