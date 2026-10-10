import asyncio
import json

import httpx
import pytest

from custom_components.codex_assist.codex_client import (
    MAX_SEARCH_CITATIONS,
    MAX_SEARCH_OUTPUT_CHARS,
    MAX_SEARCH_ROUNDS,
    CodexAuthenticationError,
    CodexCitationDelta,
    CodexClient,
    CodexRateLimitError,
    CodexResponseItemDelta,
    CodexTextDelta,
    CodexToolCallDelta,
)


class FakeStreamResponse:
    def __init__(self, lines):
        self.status_code = 200
        self._lines = lines
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class GatedStreamResponse(FakeStreamResponse):
    """Keep response EOF pending so tests can observe delivery and cancellation."""

    def __init__(self, lines):
        super().__init__(lines)
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def aiter_lines(self):
        async for line in super().aiter_lines():
            yield line
        self.waiting.set()
        await self.release.wait()


class FakeSearchResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = "" if status_code == 200 else json.dumps(payload)

    def json(self):
        return self._payload


class FakeHttpClient:
    """Serves one stream per model round and one reply per alpha/search call."""

    def __init__(self, streams, searches=()):
        self.streams = list(streams)
        self.searches = list(searches)
        self.stream_calls = []
        self.search_calls = []

    async def post(self, url, **kwargs):
        self.search_calls.append((url, kwargs))
        response = self.searches.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def stream(self, method, url, **kwargs):
        self.stream_calls.append((method, url, kwargs))
        return self.streams.pop(0)


def _event(payload):
    return ["data: " + json.dumps(payload), ""]


def _round(*items):
    return FakeStreamResponse(
        sum((_event({"type": "response.output_item.done", "item": item}) for item in items), [])
    )


def _function_call(call_id, name, arguments):
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }


def _message(text):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _text_round(text):
    return FakeStreamResponse(
        _event({"type": "response.output_text.delta", "delta": text})
        + _event({"type": "response.output_item.done", "item": _message(text)})
    )


SEARCH_OUTPUT = (
    "IANA Reserved Domains (https://www.iana.org/help/example-domains)\n"
    "\ue200cite\ue202turn0search0\ue201 [wordlim: 200] Example domains are maintained by IANA."
)
SEARCH_PAYLOAD = {
    "output": SEARCH_OUTPUT,
    "results": [
        {
            "type": "text_result",
            "ref_id": "turn0search0",
            "title": "IANA Reserved Domains",
            "url": "https://www.iana.org/help/example-domains",
        }
    ],
    "encrypted_output": "opaque",
}


async def _collect(client, tools, **kwargs):
    return [
        delta
        async for delta in client.stream_turn(
            model="gpt-test",
            instructions="Use search.",
            input_items=[{"role": "user", "content": "Who maintains Example Domains?"}],
            tools=tools,
            **kwargs,
        )
    ]


@pytest.mark.asyncio
async def test_stream_turn_replaces_hosted_search_with_client_side_function():
    http = FakeHttpClient([_text_round("No search needed.")])
    client = CodexClient(http_client=http, access_token="token-1")

    deltas = await _collect(client, [{"type": "web_search"}])

    payload = http.stream_calls[0][2]["json"]
    assert [(tool["type"], tool["name"]) for tool in payload["tools"]] == [
        ("function", "web_search")
    ]
    assert payload["tools"][0]["parameters"]["required"] == ["queries"]
    assert "web_search_call.action.sources" not in payload.get("include", [])
    assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == [
        "No search needed."
    ]
    assert http.search_calls == []


@pytest.mark.asyncio
async def test_stream_turn_runs_search_and_hides_search_round_from_caller():
    search_call = _function_call("call-s1", "web_search", {"queries": ["example domains"]})
    reasoning = {"type": "reasoning", "encrypted_content": "round-1"}
    http = FakeHttpClient(
        [_round(reasoning, search_call), _text_round("IANA maintains them.")],
        [FakeSearchResponse(200, SEARCH_PAYLOAD)],
    )
    client = CodexClient(http_client=http, access_token="token-1")
    location = {"type": "approximate", "country": "US"}

    deltas = await _collect(client, [{"type": "web_search", "user_location": location}])

    url, kwargs = http.search_calls[0]
    assert url == "https://chatgpt.com/backend-api/codex/alpha/search"
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["json"]["model"] == "gpt-test"
    assert kwargs["json"]["commands"] == {"search_query": [{"q": "example domains"}]}
    assert kwargs["json"]["settings"]["user_location"] == location
    assert kwargs["json"]["settings"]["external_web_access"] is True

    second_input = http.stream_calls[1][2]["json"]["input"]
    assert second_input[-3:-1] == [reasoning, search_call]
    assert second_input[-1]["type"] == "function_call_output"
    assert second_input[-1]["call_id"] == "call-s1"
    assert second_input[-1]["output"] == (
        "IANA Reserved Domains (https://www.iana.org/help/example-domains)\n"
        "Example domains are maintained by IANA."
    )

    assert not any(isinstance(delta, CodexToolCallDelta) for delta in deltas)
    assert [delta.item for delta in deltas if isinstance(delta, CodexResponseItemDelta)] == [
        *second_input[1:], _message("IANA maintains them.")
    ]
    assert [
        (delta.citation.title, delta.citation.url)
        for delta in deltas
        if isinstance(delta, CodexCitationDelta)
    ] == [("IANA Reserved Domains", "https://www.iana.org/help/example-domains")]


@pytest.mark.asyncio
@pytest.mark.parametrize("search_first", [False, True])
async def test_stream_turn_discards_search_round_text_and_annotations(search_first):
    search_call = _function_call("call-s1", "web_search", {"queries": ["example domains"]})
    preamble = _message("Let me look that up.")
    reasoning = {"type": "reasoning", "encrypted_content": "search-round"}
    text_events = (
        _event({"type": "response.output_text.delta", "delta": "Let me look that up."})
        + _event(
            {
                "type": "response.output_text.annotation.added",
                "annotation": {
                    "type": "url_citation",
                    "title": "Internal preamble citation",
                    "url": "https://example.org/preamble",
                },
            }
        )
        + _event({"type": "response.output_item.done", "item": preamble})
    )
    search_events = _event({"type": "response.output_item.done", "item": search_call})
    first_round = FakeStreamResponse(
        _event({"type": "response.output_item.done", "item": reasoning})
        + (search_events + text_events if search_first else text_events + search_events)
    )
    final_text = "IANA maintains them."
    final_reasoning = {"type": "reasoning", "encrypted_content": "final-round"}
    final_round = FakeStreamResponse(
        _event({"type": "response.output_item.done", "item": final_reasoning})
        + _text_round(final_text)._lines
    )
    http = FakeHttpClient(
        [first_round, final_round], [FakeSearchResponse(200, SEARCH_PAYLOAD)]
    )
    client = CodexClient(http_client=http, access_token="token-1")

    deltas = await _collect(client, [{"type": "web_search"}])

    assert [delta.item for delta in deltas if isinstance(delta, CodexResponseItemDelta)] == [
        *http.stream_calls[1][2]["json"]["input"][1:], final_reasoning, _message(final_text)
    ]
    assert not any(isinstance(delta, CodexToolCallDelta) for delta in deltas)
    assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == [final_text]
    assert [delta.citation.title for delta in deltas if isinstance(delta, CodexCitationDelta)] == [
        "IANA Reserved Domains"
    ]
    replay = http.stream_calls[1][2]["json"]["input"]
    assert replay[1:-1] == (
        [reasoning, search_call, preamble] if search_first else [reasoning, preamble, search_call]
    )
    assert replay[-1]["type"] == "function_call_output"
    assert replay[-1]["call_id"] == "call-s1"


@pytest.mark.asyncio
@pytest.mark.parametrize("search_first", [False, True])
async def test_stream_turn_lets_home_assistant_tool_win_over_search_in_same_round(search_first):
    search_call = _function_call("call-s1", "web_search", {"queries": ["weather"]})
    ha_call = _function_call("call-h1", "HassTurnOn", {"name": "Kitchen"})
    calls = [search_call, ha_call] if search_first else [ha_call, search_call]
    message = _message("Using a Home Assistant tool.")
    reasoning = {"type": "reasoning", "encrypted_content": "mixed-round"}
    http = FakeHttpClient(
        [
            FakeStreamResponse(
                _round(reasoning)._lines
                + _text_round("Using a Home Assistant tool.")._lines + _round(*calls)._lines
            )
        ]
    )
    client = CodexClient(http_client=http, access_token="token-1")

    deltas = await _collect(
        client,
        [{"type": "function", "name": "HassTurnOn", "parameters": {}}, {"type": "web_search"}],
    )

    assert http.search_calls == []
    assert len(http.stream_calls) == 1
    assert [delta.tool_call.name for delta in deltas if isinstance(delta, CodexToolCallDelta)] == [
        "HassTurnOn"
    ]
    assert [delta.item for delta in deltas if isinstance(delta, CodexResponseItemDelta)] == [
        reasoning, message, *calls,
        {
            "type": "function_call_output", "call_id": "call-s1",
            "output": "Web search skipped because Home Assistant tools take priority.",
        },
    ]
    assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == [
        "Using a Home Assistant tool."
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("unexpected_search", [False, True])
async def test_stream_turn_drops_search_tool_after_round_limit(unexpected_search):
    rounds = [
        _round(_function_call(f"call-{n}", "web_search", {"queries": [f"q{n}"]}))
        for n in range(MAX_SEARCH_ROUNDS)
    ]
    last_round = _text_round("Best answer so far.")
    if unexpected_search:
        last_round._lines += _round(
            _function_call("unexpected", "web_search", {"queries": ["one more"]})
        )._lines
    http = FakeHttpClient(
        [*rounds, last_round],
        [FakeSearchResponse(200, SEARCH_PAYLOAD) for _ in range(MAX_SEARCH_ROUNDS)],
    )
    client = CodexClient(http_client=http, access_token="token-1")

    if unexpected_search:
        with pytest.raises(RuntimeError, match="search.*disabled"):
            await _collect(client, [{"type": "web_search"}])
        assert len(http.search_calls) == MAX_SEARCH_ROUNDS
        return
    deltas = await _collect(client, [{"type": "web_search"}])

    assert len(http.search_calls) == MAX_SEARCH_ROUNDS
    assert len(http.stream_calls) == MAX_SEARCH_ROUNDS + 1
    assert "tools" not in http.stream_calls[-1][2]["json"]
    assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == [
        "Best answer so far."
    ]
    assert not any(isinstance(delta, CodexToolCallDelta) for delta in deltas)
    assert [delta.item for delta in deltas if isinstance(delta, CodexResponseItemDelta)] == [
        *http.stream_calls[-1][2]["json"]["input"][1:], _message("Best answer so far.")
    ]


@pytest.mark.asyncio
async def test_web_search_sends_open_urls_and_limits_output_and_citations():
    results = [
        {"title": f"Page {n}", "url": f"https://example.com/{n}"}
        for n in range(MAX_SEARCH_CITATIONS + 3)
    ]
    payload = {
        "output": "x" * (MAX_SEARCH_OUTPUT_CHARS + 50),
        "results": [{"title": "", "url": "https://example.com/untitled"}, "junk", *results],
    }
    http = FakeHttpClient([], [FakeSearchResponse(200, payload)])
    client = CodexClient(http_client=http, access_token="token-1")

    result = await client.web_search(
        "gpt-test", {"queries": ["a", " ", 3], "open_urls": ["https://example.com/0"]}, {}
    )

    assert http.search_calls[0][1]["json"]["commands"] == {
        "search_query": [{"q": "a"}],
        "open": [{"ref_id": "https://example.com/0"}],
    }
    assert len(result.text) == MAX_SEARCH_OUTPUT_CHARS
    assert [citation.url for citation in result.citations] == [
        result["url"] for result in results[:MAX_SEARCH_CITATIONS]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"output": None}, {"output": []}, {"output": 3}, [], "text"]
)
async def test_web_search_rejects_malformed_success_payload(payload):
    http = FakeHttpClient([], [FakeSearchResponse(200, payload)])
    client = CodexClient(http_client=http, access_token="token-1")
    result = await client.web_search("gpt-test", {"queries": ["a"]}, {})
    assert result.text == "Web search failed: invalid response output."
    assert "found nothing" not in result.text
    assert result.citations == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [None, []])
@pytest.mark.parametrize("output", ["Output-only evidence.", "", "  "])
async def test_web_search_accepts_valid_output_without_result_cards(output, results):
    payload = {"output": output}
    if results is not None:
        payload["results"] = results
    http = FakeHttpClient([], [FakeSearchResponse(200, payload)])
    client = CodexClient(http_client=http, access_token="token-1")
    result = await client.web_search("gpt-test", {"queries": ["a"]}, {})
    assert result.text == (output if output.strip() else "Web search found nothing.")
    assert result.citations == ()


@pytest.mark.asyncio
async def test_web_search_reports_failures_to_the_model():
    http = FakeHttpClient(
        [],
        [
            FakeSearchResponse(500, {"detail": "search backend down"}),
            FakeSearchResponse(200, {"output": "  ", "results": []}),
        ],
    )
    client = CodexClient(http_client=http, access_token="token-1")

    assert (await client.web_search("gpt-test", {"queries": []}, {})).text == (
        "web_search needs at least one query."
    )
    failed = await client.web_search("gpt-test", {"queries": ["a"]}, {})
    assert failed.text == "Web search failed (HTTP 500): search backend down"
    assert failed.citations == ()
    assert (await client.web_search("gpt-test", {"queries": ["a"]}, {})).text == (
        "Web search found nothing."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (401, None, CodexAuthenticationError),
        (403, "token_invalidated", CodexAuthenticationError),
        (429, None, CodexRateLimitError),
        (403, "usage_limit_reached", CodexRateLimitError),
        (500, "rate_limit_exceeded", CodexRateLimitError),
        (401, "rate_limit_exceeded", CodexAuthenticationError),
    ],
)
async def test_web_search_raises_auth_and_rate_limit_errors(status, code, error):
    http = FakeHttpClient(
        [], [FakeSearchResponse(status, {"error": {"message": "nope", "code": code}})]
    )
    client = CodexClient(http_client=http, access_token="token-1")

    with pytest.raises(error):
        await client.web_search("gpt-test", {"queries": ["a"]}, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["search_off", "search_enabled", "limit", "ha_tool"])
async def test_stream_turn_streams_only_when_round_is_known_to_be_terminal(path):
    lines = (
        _event({"type": "response.output_text.delta", "delta": "Hel"})
        + _event({"type": "response.output_text.delta", "delta": "lo"})
        + _round(_message("Hello"))._lines
    )
    ha_call = _function_call("ha-1", "HassTurnOn", {"name": "Kitchen"})
    if path == "ha_tool":
        lines += _round(ha_call)._lines
    response = GatedStreamResponse(lines)
    rounds = [
        _round(_function_call(f"s-{n}", "web_search", {"queries": ["example domains"]}))
        for n in range(MAX_SEARCH_ROUNDS if path == "limit" else 0)
    ]
    http = FakeHttpClient(
        [*rounds, response],
        [FakeSearchResponse(200, {"output": "result"}) for _ in rounds],
    )
    client = CodexClient(http_client=http, access_token="token-1")
    tools: list[dict[str, object]] = [] if path == "search_off" else [{"type": "web_search"}]
    if path == "ha_tool":
        tools.append({"type": "function", "name": "HassTurnOn", "parameters": {}})
    deltas = []

    async def consume():
        async for delta in client.stream_turn(
            model="gpt-test", instructions="Test", input_items=[], tools=tools
        ):
            deltas.append(delta)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(response.waiting.wait(), timeout=2)
        assert not response.closed
        assert not task.done()
        assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == (
            [] if path == "search_enabled" else ["Hel", "lo"]
        )
    finally:
        response.release.set()
        await asyncio.wait_for(task, timeout=2)

    assert response.closed
    assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == ["Hel", "lo"]
    expected_native = [*http.stream_calls[-1][2]["json"]["input"], _message("Hello")]
    if path == "ha_tool":
        expected_native.append(ha_call)
    assert [delta.item for delta in deltas if isinstance(delta, CodexResponseItemDelta)] == (
        expected_native
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        {"type": "response.failed", "response": {"error": {"message": "backend down"}}},
        {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "limit"}}},
        {"type": "error", "message": "backend down"},
    ],
)
async def test_stream_turn_failed_round_does_not_publish_buffered_text(failure):
    response = FakeStreamResponse(_text_round("Unfinished answer.")._lines + _event(failure))
    http = FakeHttpClient([response])
    client = CodexClient(http_client=http, access_token="token-1")
    deltas = []

    with pytest.raises(RuntimeError, match="backend down|response incomplete: limit"):
        async for delta in client.stream_turn(
            model="gpt-test", instructions="Test", input_items=[], tools=[{"type": "web_search"}]
        ):
            deltas.append(delta)

    assert deltas == []
    assert response.closed
    assert len(http.stream_calls) == 1
    assert http.search_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "auth", "rate_limit", "server"])
async def test_stream_turn_handles_search_failure_without_leaking_preamble(failure):
    search_call = _function_call("s-1", "web_search", {"queries": ["example domains"]})
    response = FakeStreamResponse(_text_round("Searching.")._lines + _round(search_call)._lines)
    errors = {
        "timeout": httpx.ReadTimeout("search timed out"),
        "auth": FakeSearchResponse(401, {"detail": "nope"}),
        "rate_limit": FakeSearchResponse(429, {"detail": "nope"}),
        "server": FakeSearchResponse(500, {"detail": "search backend down"}),
    }
    http = FakeHttpClient([response, _text_round("Search is unavailable.")], [errors[failure]])
    client = CodexClient(http_client=http, access_token="token-1")
    deltas = []

    async def consume():
        async for delta in client.stream_turn(
            model="gpt-test", instructions="Test", input_items=[], tools=[{"type": "web_search"}]
        ):
            deltas.append(delta)

    if failure == "server":
        await consume()
        assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == [
            "Search is unavailable."
        ]
        assert http.stream_calls[1][2]["json"]["input"][-1] == {
            "type": "function_call_output",
            "call_id": "s-1",
            "output": "Web search failed (HTTP 500): search backend down",
        }
    else:
        expected = {
            "timeout": httpx.ReadTimeout,
            "auth": CodexAuthenticationError,
            "rate_limit": CodexRateLimitError,
        }[failure]
        with pytest.raises(expected):
            await consume()
        assert deltas == []
        assert len(http.stream_calls) == 1
    assert response.closed
    assert len(http.search_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["response", "search"])
async def test_stream_turn_cancellation_stops_internal_round(stage, monkeypatch):
    search_call = _function_call("s-1", "web_search", {"queries": ["example domains"]})
    lines = _text_round("Searching.")._lines + _round(search_call)._lines
    if stage == "response":
        response = GatedStreamResponse(lines)
        waiting = response.waiting
    else:
        response = FakeStreamResponse(lines)
        waiting = asyncio.Event()
    http = FakeHttpClient([response])
    search_finished = asyncio.Event()

    async def blocked_post(url, **kwargs):
        http.search_calls.append((url, kwargs))
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            search_finished.set()

    if stage == "search":
        monkeypatch.setattr(http, "post", blocked_post)
    client = CodexClient(http_client=http, access_token="token-1")
    deltas = []

    async def consume():
        async for delta in client.stream_turn(
            model="gpt-test", instructions="Test", input_items=[], tools=[{"type": "web_search"}]
        ):
            deltas.append(delta)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert deltas == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert response.closed
    assert len(http.stream_calls) == 1
    assert len(http.search_calls) == (1 if stage == "search" else 0)
    assert search_finished.is_set() == (stage == "search")


@pytest.mark.asyncio
async def test_stream_turn_rejects_search_batch_over_total_budget():
    # A round count alone does not bound the number of backend searches.
    calls = [
        _function_call(f"search-{n}", "web_search", {"queries": [f"query-{n}"]})
        for n in range(5)
    ]
    http = FakeHttpClient(
        [_round(*calls), _text_round("Should not execute this batch.")],
        [FakeSearchResponse(200, SEARCH_PAYLOAD) for _ in calls],
    )
    client = CodexClient(http_client=http, access_token="token-1")
    with pytest.raises(RuntimeError, match="search.*budget"):
        await _collect(client, [{"type": "web_search"}])
    assert http.search_calls == []


@pytest.mark.asyncio
async def test_stream_turn_disables_search_after_total_call_budget():
    # Two searches per model round exhaust the four-call budget in two rounds.
    rounds = [
        _round(*[
            _function_call(f"search-{n}-{i}", "web_search", {"queries": ["query"]})
            for i in range(2)
        ])
        for n in range(2)
    ]
    http = FakeHttpClient(
        [*rounds, _text_round("Final answer.")],
        [FakeSearchResponse(200, SEARCH_PAYLOAD) for _ in range(4)],
    )
    client = CodexClient(http_client=http, access_token="token-1")
    deltas = await _collect(client, [{"type": "web_search"}])
    assert len(http.search_calls) == 4
    assert "tools" not in http.stream_calls[-1][2]["json"]
    assert [d.text for d in deltas if isinstance(d, CodexTextDelta)] == ["Final answer."]
