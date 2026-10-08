"""AI Task delivery through real HA APIs, services, and local media.

Only external Codex HTTP is stubbed. HA owns task creation, feature admission,
chat logs, selector conversion, result serialization, and image persistence.
These contracts do not prove live authentication, backend, or device behavior.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from homeassistant.components import ai_task, media_source
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_assist import DOMAIN

ENTITY_ID = "ai_task.codex_assist_ai_task"
# A synthetic 1x1 PNG, not a camera snapshot or a real backend output.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNoaGgAAAMEAYFL"
    "09IQAAAAAElFTkSuQmCC"
)


@pytest.fixture
async def task_entry(hass: HomeAssistant, tmp_path: Path) -> MockConfigEntry:
    """Load the provider and real media sources into an isolated HA instance."""
    hass.config.media_dirs = {"local": str(tmp_path)}
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "media_source", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Codex Assist",
        unique_id=DOMAIN,
        data={
            # Non-expiring synthetic credential: real coordination, no auth HTTP.
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "model": "gpt-5.4",
        },
        options={"image_model": "gpt-image-2-high", "image_size": "1536x1024"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def backend(monkeypatch: pytest.MonkeyPatch):
    """Supply SSE at the external HTTP boundary and capture the real request."""
    requests: list[dict] = []
    events: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            "custom_components.codex_assist.ai_task.get_async_client", lambda hass: client
        )
        yield requests, events


def _reply(events: list[dict], text: str) -> None:
    events.append({"type": "response.output_text.delta", "delta": text})


async def test_generate_data_returns_native_plain_text_result(
    hass: HomeAssistant, task_entry: MockConfigEntry, backend
) -> None:
    """GENERATE_DATA must admit and deliver a real HA result, not just a flag."""
    requests, events = backend
    _reply(events, "A concise task response.")

    result = await ai_task.async_generate_data(
        hass,
        task_name="Plain task",
        entity_id=ENTITY_ID,
        instructions="Summarize the task.",
    )

    assert isinstance(result, ai_task.GenDataTaskResult)
    assert result.data == "A concise task response."
    assert result.conversation_id
    assert result.as_dict() == {
        "conversation_id": result.conversation_id,
        "data": "A concise task response.",
    }
    assert len(requests) == 1
    assert requests[0]["input"] == [{"role": "user", "content": "Summarize the task."}]
    assert "format" not in requests[0].get("text", {})


async def test_generate_data_service_delivers_selector_validated_structure(
    hass: HomeAssistant, task_entry: MockConfigEntry, backend
) -> None:
    """Service selectors must reach Codex as schema and return data, not JSON text."""
    hass.config_entries.async_update_entry(task_entry, options={"web_search": True})
    requests, events = backend
    _reply(events, '{"summary":"All clear.","note":null}')

    result = await hass.services.async_call(
        "ai_task",
        "generate_data",
        {
            "task_name": "Status report",
            "entity_id": ENTITY_ID,
            "instructions": "Return a status report.",
            "structure": {
                "summary": {"required": True, "selector": {"text": {}}},
                "note": {"selector": {"text": {}}},
            },
        },
        blocking=True,
        return_response=True,
    )

    assert result is not None
    assert result["data"] == {"summary": "All clear."}
    assert result["conversation_id"]
    assert len(requests) == 1
    assert requests[0]["input"] == [{"role": "user", "content": "Return a status report."}]
    assert requests[0]["text"]["format"] == {
        "type": "json_schema",
        "name": "status_report",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "note": {"type": ["string", "null"]},
            },
            "required": ["summary", "note"],
            "additionalProperties": False,
        },
    }
    assert not any(tool["type"] == "web_search" for tool in requests[0].get("tools", []))


async def test_generate_data_service_sends_local_image_attachment(
    hass: HomeAssistant, task_entry: MockConfigEntry, tmp_path: Path, backend
) -> None:
    """SUPPORT_ATTACHMENTS must survive HA media resolution and reach backend HTTP."""
    await hass.async_add_executor_job((tmp_path / "attachment.png").write_bytes, PNG_BYTES)
    requests, events = backend
    _reply(events, "The supplied image was received.")

    result = await hass.services.async_call(
        "ai_task",
        "generate_data",
        {
            "task_name": "Image description",
            "entity_id": ENTITY_ID,
            "instructions": "Describe the supplied image.",
            "attachments": [
                {
                    "media_content_id": "media-source://media_source/local/attachment.png",
                    "media_content_type": "image/png",
                }
            ],
        },
        blocking=True,
        return_response=True,
    )

    assert result is not None
    assert result["data"] == "The supplied image was received."
    assert result["conversation_id"]
    assert len(requests) == 1
    content = requests[0]["input"][0]["content"]
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0] == {"type": "input_text", "text": "Describe the supplied image."}
    assert content[1]["type"] == "input_image"
    prefix, encoded = content[1]["image_url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    assert base64.b64decode(encoded, validate=True) == PNG_BYTES


async def test_generate_image_service_persists_native_result_as_media(
    hass: HomeAssistant, task_entry: MockConfigEntry, tmp_path: Path, backend
) -> None:
    """GENERATE_IMAGE must deliver native metadata, persisted bytes, and signed media."""
    requests, events = backend
    _reply(events, "A simple synthetic landscape.")
    events.append(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "image_generation_call",
                "result": base64.b64encode(PNG_BYTES).decode(),
            },
        }
    )

    result = await hass.services.async_call(
        "ai_task",
        "generate_image",
        {
            "task_name": "Test landscape",
            "entity_id": ENTITY_ID,
            "instructions": "Draw a simple landscape.",
        },
        blocking=True,
        return_response=True,
    )

    assert result is not None
    assert result["conversation_id"]
    assert result["mime_type"] == "image/png"
    assert (result["width"], result["height"]) == (1536, 1024)
    assert result["model"] == "gpt-image-2-high"
    assert result["revised_prompt"] == "A simple synthetic landscape."
    assert "image_data" not in result
    assert result["media_source_id"].startswith("media-source://ai_task/image/")
    media = await media_source.async_resolve_media(hass, result["media_source_id"], None)
    assert media.mime_type == "image/png"
    assert media.path is not None
    assert media.path.parent == tmp_path / "ai_task" / "image"
    assert await hass.async_add_executor_job(media.path.read_bytes) == PNG_BYTES
    signed_url = urlsplit(result["url"])
    assert signed_url.path == media.url
    assert parse_qs(signed_url.query)["authSig"]
    assert len(requests) == 1
    assert requests[0]["input"] == [{"role": "user", "content": "Draw a simple landscape."}]
    image_tool = next(tool for tool in requests[0]["tools"] if tool["type"] == "image_generation")
    assert image_tool["size"] == "1536x1024"
    assert image_tool["quality"] == "high"
