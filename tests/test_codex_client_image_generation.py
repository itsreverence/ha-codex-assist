import base64
import json

import pytest

from custom_components.codex_assist.codex_client import CodexClient


class FakeStreamResponse:
    def __init__(self, status_code=200, lines=None, error_body=b""):
        self.status_code = status_code
        self._lines = lines or []
        self._error_body = error_body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self._error_body


class FakeStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, **kwargs):
        raise AssertionError("generate_image should stream, not post")

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeStreamContext(self.response)


def _event(event_type, payload):
    return [
        f"event: {event_type}",
        f"data: {json.dumps(payload)}",
        "",
    ]


@pytest.mark.asyncio
async def test_generate_image_uses_codex_responses_image_generation_tool():
    image_data = b"fake-png-bytes"
    image_b64 = base64.b64encode(image_data).decode("ascii")
    response = FakeStreamResponse(
        lines=(
            _event("response.output_text.delta", {"delta": "A generated image."})
            + _event(
                "response.output_item.done",
                {
                    "item": {
                        "type": "image_generation_call",
                        "result": image_b64,
                    }
                },
            )
            + ["data: [DONE]", ""]
        )
    )
    http = FakeHttpClient(response)
    client = CodexClient(http_client=http, access_token="token-1")

    result = await client.generate_image(
        prompt="draw a tidy smart home dashboard icon",
        input_items=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "draw a tidy smart home dashboard icon"}
                ],
            }
        ],
        chat_model="gpt-5.4",
        image_model="gpt-image-2-high",
        size="1536x1024",
    )

    assert result.image_data == image_data
    assert result.mime_type == "image/png"
    assert result.model == "gpt-image-2-high"
    assert result.revised_prompt == "A generated image."

    method, url, kwargs = http.calls[0]
    payload = kwargs["json"]
    assert method == "POST"
    assert url == "https://chatgpt.com/backend-api/codex/responses"
    assert kwargs["headers"]["Accept"] == "text/event-stream"
    assert payload["model"] == "gpt-5.4"
    assert payload["stream"] is True
    assert payload["tools"] == [
        {
            "type": "image_generation",
            "model": "gpt-image-2",
            "size": "1536x1024",
            "quality": "high",
            "output_format": "png",
            "background": "opaque",
            "partial_images": 1,
        }
    ]
    assert payload["tool_choice"] == {
        "type": "allowed_tools",
        "mode": "required",
        "tools": [{"type": "image_generation"}],
    }


@pytest.mark.asyncio
async def test_generate_image_accepts_partial_image_b64_events_without_type():
    image_data = b"partial-image"
    payload = {"partial_image_b64": base64.b64encode(image_data).decode("ascii")}
    response = FakeStreamResponse(
        lines=[
            "event: response.image_generation_call.partial_image",
            f"data: {json.dumps(payload)}",
            "",
        ]
    )
    client = CodexClient(
        http_client=FakeHttpClient(response),
        access_token="token-1",
    )

    result = await client.generate_image(prompt="draw it")

    assert result.image_data == image_data


@pytest.mark.asyncio
async def test_generate_image_rejects_unsupported_image_options_before_request():
    client = CodexClient(
        http_client=FakeHttpClient(FakeStreamResponse()),
        access_token="token-1",
    )

    with pytest.raises(ValueError, match="Unsupported Codex Assist image model"):
        await client.generate_image(prompt="draw it", image_model="gpt-image-2-ultra")

    with pytest.raises(ValueError, match="Unsupported Codex Assist image size"):
        await client.generate_image(prompt="draw it", size="2048x2048")


@pytest.mark.asyncio
async def test_generate_image_raises_when_stream_has_no_image_result():
    client = CodexClient(
        http_client=FakeHttpClient(
            FakeStreamResponse(lines=_event("response.completed", {"type": "response.completed"}))
        ),
        access_token="token-1",
    )

    with pytest.raises(RuntimeError, match="no image_generation result"):
        await client.generate_image(prompt="draw it")
