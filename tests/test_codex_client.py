import pytest

from custom_components.codex_assist.codex_client import CodexClient, CodexMessage


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_generate_text_posts_responses_payload_to_codex_backend():
    http = FakeHttpClient(
        [
            FakeResponse(
                200,
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Done."}],
                        }
                    ]
                },
            )
        ]
    )
    client = CodexClient(http_client=http, access_token="token-1")

    result = await client.generate_text(
        model="gpt-5.4",
        instructions="You are a Home Assistant voice agent.",
        messages=[CodexMessage(role="user", content="turn on movie mode")],
    )

    assert result == "Done."
    assert http.calls[0][0] == "https://chatgpt.com/backend-api/codex/responses"
    assert http.calls[0][1]["headers"]["Authorization"] == "Bearer token-1"
    assert http.calls[0][1]["json"] == {
        "model": "gpt-5.4",
        "instructions": "You are a Home Assistant voice agent.",
        "input": [{"role": "user", "content": "turn on movie mode"}],
        "store": False,
    }


@pytest.mark.asyncio
async def test_generate_text_extracts_concatenated_output_text_items():
    http = FakeHttpClient([
        FakeResponse(
            200,
            {
                "output": [
                    {"type": "reasoning", "summary": []},
                    {"type": "message", "content": [
                        {"type": "output_text", "text": "First"},
                        {"type": "output_text", "text": " second."},
                    ]},
                ]
            },
        )
    ])
    client = CodexClient(http_client=http, access_token="token-1")

    result = await client.generate_text(
        model="gpt-5.4",
        instructions="x",
        messages=[CodexMessage(role="user", content="hello")],
    )

    assert result == "First second."


@pytest.mark.asyncio
async def test_generate_text_raises_clear_error_for_backend_failure():
    http = FakeHttpClient([FakeResponse(429, {"error": "quota"})])
    client = CodexClient(http_client=http, access_token="token-1")

    with pytest.raises(RuntimeError, match="Codex request failed with status 429"):
        await client.generate_text(
            model="gpt-5.4",
            instructions="x",
            messages=[CodexMessage(role="user", content="hello")],
        )
