import json

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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


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
        return self.searches.pop(0)

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
        _message("IANA maintains them.")
    ]
    assert [
        (delta.citation.title, delta.citation.url)
        for delta in deltas
        if isinstance(delta, CodexCitationDelta)
    ] == [("IANA Reserved Domains", "https://www.iana.org/help/example-domains")]


@pytest.mark.asyncio
async def test_stream_turn_lets_home_assistant_tool_win_over_search_in_same_round():
    search_call = _function_call("call-s1", "web_search", {"queries": ["weather"]})
    ha_call = _function_call("call-h1", "HassTurnOn", {"name": "Kitchen"})
    http = FakeHttpClient([_round(search_call, ha_call)])
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
        ha_call
    ]


@pytest.mark.asyncio
async def test_stream_turn_drops_search_tool_after_round_limit():
    rounds = [
        _round(_function_call(f"call-{n}", "web_search", {"queries": [f"q{n}"]}))
        for n in range(MAX_SEARCH_ROUNDS)
    ]
    http = FakeHttpClient(
        [*rounds, _text_round("Best answer so far.")],
        [FakeSearchResponse(200, SEARCH_PAYLOAD) for _ in range(MAX_SEARCH_ROUNDS)],
    )
    client = CodexClient(http_client=http, access_token="token-1")

    deltas = await _collect(client, [{"type": "web_search"}])

    assert len(http.search_calls) == MAX_SEARCH_ROUNDS
    assert len(http.stream_calls) == MAX_SEARCH_ROUNDS + 1
    assert "tools" not in http.stream_calls[-1][2]["json"]
    assert [delta.text for delta in deltas if isinstance(delta, CodexTextDelta)] == [
        "Best answer so far."
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
    ("status", "error"),
    [(401, CodexAuthenticationError), (429, CodexRateLimitError)],
)
async def test_web_search_raises_auth_and_rate_limit_errors(status, error):
    http = FakeHttpClient([], [FakeSearchResponse(status, {"detail": "nope"})])
    client = CodexClient(http_client=http, access_token="token-1")

    with pytest.raises(error):
        await client.web_search("gpt-test", {"queries": ["a"]}, {})
