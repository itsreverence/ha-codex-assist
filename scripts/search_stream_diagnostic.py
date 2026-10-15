from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from dataclasses import asdict, dataclass, field
from threading import Lock

import httpx

from custom_components.codex_assist import codex_client
from scripts import probe_search_comparison as probe

_SCOPE_LOCK = Lock()
ITEM_TYPES = (
    "message", "reasoning", "web_search_call", "function_call", "function_call_output",
)
EVENT_TYPES = (
    "response.created", "response.in_progress", "response.output_item.added",
    "response.output_item.done", "response.output_text.delta",
    "response.output_text.done", "response.content_part.added", "response.content_part.done",
    "response.function_call_arguments.delta", "response.function_call_arguments.done",
    "response.completed", "error", "response.failed", "response.incomplete",
)
TERMINALS = {
    "response.completed": "completed", "error": "event_error",
    "response.failed": "event_failed", "response.incomplete": "event_incomplete",
}


class ProbeObserverError(RuntimeError):
    pass


class ProbeBudgetError(ProbeObserverError):
    pass


def bucket(value, allowed):
    return value if isinstance(value, str) and value in allowed else "other"


def replay_shape(payload):
    counts = dict.fromkeys((*ITEM_TYPES, "other"), 0)
    prefix = []
    presence = dict.fromkeys(("id", "status", "action"), 0)
    encrypted_present = encrypted_nonempty = 0
    items = payload.get("input", [])
    for item in items:
        kind = bucket(item.get("type") if isinstance(item, dict) else None, ITEM_TYPES)
        counts[kind] += 1
        if len(prefix) < 32:
            prefix.append(kind)
        if not isinstance(item, dict):
            continue
        for key in presence:
            presence[key] += int(key in item)
        if kind == "reasoning":
            encrypted_present += int("encrypted_content" in item)
            value = item.get("encrypted_content")
            encrypted_nonempty += int(isinstance(value, str) and bool(value))
    return {
        "item_types": counts, "ordered_prefix": prefix,
        "omitted_items": max(0, len(items) - 32), "field_presence": presence,
        "reasoning_encrypted_present": encrypted_present,
        "reasoning_encrypted_nonempty": encrypted_nonempty,
        "include_encrypted_reasoning": "reasoning.encrypted_content" in payload.get("include", []),
        "tools_count": len(payload.get("tools", [])), "store": payload.get("store") is True,
    }


@dataclass
class StreamTrace:
    stage: str = "not_started"
    outcome: str = "not_run"
    header_received: bool = False
    status_class: str = "none"
    http_200: bool = False
    decoded_lines: int = 0
    events: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys((*EVENT_TYPES, "other"), 0),
    )
    terminal_kind: str = "none"
    natural_eof: bool = False
    replay: dict | None = None


def outcome(exc, trace):
    if isinstance(exc, codex_client.CodexAuthenticationError):
        return "authentication"
    if isinstance(exc, codex_client.CodexRateLimitError):
        return "rate_limit"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.StreamError):
        return "stream_error"
    if isinstance(exc, httpx.HTTPError):
        return "transport_error"
    if isinstance(exc, ProbeObserverError):
        return "probe_error"
    if trace.header_received and not trace.http_200:
        return "http_status"
    if trace.terminal_kind in {"event_error", "event_failed", "event_incomplete"}:
        return trace.terminal_kind
    if trace.natural_eof:
        if sum(trace.events.values()):
            return "eof_after_events"
        return "eof_undecoded" if trace.decoded_lines else "eof_empty"
    return "unattributed_runtime" if isinstance(exc, RuntimeError) else "unexpected"


class StreamObserver:
    def __init__(self):
        self.trace = StreamTrace()
        self.requests = 0
        self._responses = {}
        self._iterators = []

    async def __aenter__(self):
        if not _SCOPE_LOCK.acquire(blocking=False):
            raise ProbeObserverError()
        self._original = codex_client._aiter_sse_events

        def observed(response):
            trace = self._responses.get(response)
            if trace is None:
                return self._original(response)
            iterator = self._observe(response, trace)
            self._iterators.append(iterator)
            return iterator

        codex_client._aiter_sse_events = observed
        return self

    async def _observe(self, response, trace):
        async with aclosing(self._original(response)) as events:
            async for event in events:
                trace.stage = "events"
                kind = bucket(event.get("type"), EVENT_TYPES)
                trace.events[kind] += 1
                if kind in TERMINALS:
                    trace.terminal_kind = TERMINALS[kind]
                yield event

    async def request(self, request):
        if self.requests >= 2:
            raise ProbeBudgetError()
        self.trace.stage = "request"
        self.trace.replay = replay_shape(json.loads(request.content))
        self.requests += 1

    async def response(self, response):
        trace = self.trace
        self._responses[response] = trace
        trace.stage = "headers"
        trace.header_received = True
        trace.http_200 = response.status_code == 200
        trace.status_class = {1: "1xx", 2: "2xx", 3: "3xx", 4: "4xx", 5: "5xx"}.get(
            response.status_code // 100, "other",
        )
        original = response.aiter_lines

        async def decoded_lines():
            async with aclosing(original()) as decoded:
                async for line in decoded:
                    trace.decoded_lines += 1
                    yield line
                trace.natural_eof = True

        def lines():
            iterator = decoded_lines()
            self._iterators.append(iterator)
            return iterator

        response.aiter_lines = lines

    async def close_turn(self):
        failed = False
        try:
            for iterator in self._iterators:
                try:
                    await iterator.aclose()
                except Exception:
                    failed = True
        finally:
            self._iterators.clear()
            self._responses.clear()
        if failed:
            raise ProbeObserverError() from None

    async def __aexit__(self, *args):
        try:
            await self.close_turn()
        finally:
            codex_client._aiter_sse_events = self._original
            _SCOPE_LOCK.release()


async def diagnose(model, token, *, transport=None):
    initial, followup = StreamTrace(), StreamTrace()
    evidence = probe.SanitizedTurnEvidence()
    gate = "not_run"
    code = 3
    observer = StreamObserver()
    trace = initial
    deadline = asyncio.timeout(probe.ARM_SECONDS)
    try:
        async with observer, deadline, httpx.AsyncClient(
            transport=transport, timeout=probe.TURN_SECONDS,
            event_hooks={"request": [observer.request], "response": [observer.response]},
        ) as http:
            client = codex_client.CodexClient(http_client=http, access_token=token)
            original = [{"role": "user", "content": probe.CASES[0].prompt}]
            observer.trace = initial
            try:
                native = await probe.consume_turn(
                    client, "builtin", evidence, model=model,
                    input_items=original, tools=[dict(probe.SEARCH_TOOL)],
                )
                initial.outcome = "completed"
            finally:
                await observer.close_turn()
            gate = probe.initial_status(evidence, probe.CASES[0], "builtin")
            if gate == "completed":
                trace = observer.trace = followup
                evidence = probe.SanitizedTurnEvidence()
                try:
                    await probe.consume_turn(
                        client, "builtin", evidence, model=model,
                        input_items=[
                            *original, *native, {"role": "user", "content": probe.FOLLOWUP},
                        ],
                        tools=[],
                    )
                    followup.outcome = "completed"
                finally:
                    await observer.close_turn()
                gate = probe.followup_status(evidence)
            code = 0 if gate == "completed" else 1
    except Exception as exc:
        trace.outcome = outcome(exc, trace)
        gate = "error"
        code = 2 if trace.outcome in {"authentication", "rate_limit"} else (
            1 if trace.outcome in {"transport_error", "timeout"} else 3
        )
        if deadline.expired():
            code = 3
    return {
        "mode": "builtin_followup_only", "model": model, "case": probe.CASES[0].name,
        "status": {0: "completed", 1: "failed", 2: "blocked", 3: "incomplete"}[code],
        "gate": gate, "gate_pass": code == 0, "http_requests": observer.requests,
        "live_request_sent": observer.requests > 0,
        "initial": asdict(initial), "followup": asdict(followup),
    }, code
