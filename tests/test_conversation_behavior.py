from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from custom_components.codex_assist.codex_client import (
    CodexCitation,
    CodexCitationDelta,
    CodexTextDelta,
    CodexToolCall,
    CodexToolCallDelta,
)
from tests.ha_fakes import install_homeassistant_fakes


@dataclass
class FakeContent:
    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_result: dict | None = None
    tool_calls: list | None = None
    attachments: list | None = None


@dataclass
class FakeToolCall:
    id: str
    tool_name: str
    tool_args: dict


class FakeChatLog:
    def __init__(self, content=None, llm_api=None):
        self.content = content or []
        self.llm_api = llm_api
        self.streamed_entity_id = None
        self.streamed_deltas = []

    async def async_add_delta_content_stream(self, entity_id, stream):
        self.streamed_entity_id = entity_id
        async for delta in stream:
            self.streamed_deltas.append(delta)
            yield delta


class FakeCodex:
    def __init__(self, deltas):
        self.deltas = deltas
        self.calls = []

    async def _stream(self):
        for delta in self.deltas:
            yield delta

    def stream_turn(self, **kwargs):
        self.calls.append(kwargs)
        return self._stream()


class FakeHass:
    def __init__(self):
        self.executor_jobs = []

    async def async_add_executor_job(self, func, *args):
        self.executor_jobs.append((func, args))
        return func(*args)


@pytest.fixture
def conversation_module(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    module = importlib.import_module("custom_components.codex_assist.conversation")
    return importlib.reload(module)


def test_request_failure_text_names_blank_transport_error(conversation_module):
    assert conversation_module._request_failure_text(httpx.ReadTimeout("")) == (
        "Codex Assist failed: ReadTimeout"
    )
    assert conversation_module._request_failure_text(RuntimeError("backend failed")) == (
        "Codex Assist failed: backend failed"
    )


@pytest.mark.asyncio
async def test_codex_input_from_chat_log_preserves_history_tools_and_results(
    conversation_module,
):
    chat_log = FakeChatLog(
        [
            FakeContent(role="system", content="system prompt"),
            FakeContent(role="user", content="turn on kitchen"),
            FakeContent(
                role="assistant",
                content="",
                tool_calls=[
                    FakeToolCall(
                        id="call-1",
                        tool_name="HassTurnOn",
                        tool_args={"name": "Kitchen", "domain": "light"},
                    )
                ],
            ),
            FakeContent(
                role="tool_result",
                tool_call_id="call-1",
                tool_result={"success": True},
            ),
            FakeContent(role="assistant", content="Done."),
        ]
    )

    result = await conversation_module._codex_input_from_chat_log(object(), chat_log)

    assert result == [
        {"role": "user", "content": "turn on kitchen"},
        {
            "type": "function_call",
            "name": "HassTurnOn",
            "arguments": json.dumps({"name": "Kitchen", "domain": "light"}),
            "call_id": "call-1",
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": json.dumps({"success": True}),
        },
        {"role": "assistant", "content": "Done."},
    ]


@pytest.mark.asyncio
async def test_codex_stream_to_assistant_deltas_yields_text_and_tool_inputs(
    conversation_module,
):
    called = False

    def mark_called():
        nonlocal called
        called = True

    async def stream():
        yield CodexTextDelta("Working")
        yield CodexToolCallDelta(
            CodexToolCall(
                id="call-1",
                name="HassTurnOn",
                arguments={"name": "Kitchen"},
            )
        )

    deltas = [
        delta
        async for delta in conversation_module._codex_stream_to_assistant_deltas(
            stream(),
            on_tool_call=mark_called,
        )
    ]

    assert deltas[0] == {"role": "assistant"}
    assert deltas[1] == {"content": "Working"}
    assert deltas[2]["tool_calls"][0].id == "call-1"
    assert deltas[2]["tool_calls"][0].tool_name == "HassTurnOn"
    assert called is True


def test_codex_tools_from_chat_log_converts_ha_llm_api_tools(conversation_module):
    tool = type(
        "Tool",
        (),
        {
            "name": "HassTurnOn",
            "description": "Turn on a device",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
    )()
    llm_api = type("LLMApi", (), {"tools": [tool], "custom_serializer": None})()

    result = conversation_module._codex_tools_from_chat_log(FakeChatLog(llm_api=llm_api))

    assert result == [
        {
            "type": "function",
            "name": "HassTurnOn",
            "description": "Turn on a device",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
            "strict": False,
        }
    ]
    assert conversation_module._codex_tools_from_chat_log(
        FakeChatLog(llm_api=llm_api), enable_web_search=True
    ) == [*result, {"type": "web_search"}]


def test_codex_tools_adds_opt_in_web_search_without_ha_tools(conversation_module):
    assert (
        conversation_module._codex_tools_from_chat_log(FakeChatLog(), enable_web_search=False) == []
    )
    assert conversation_module._codex_tools_from_chat_log(
        FakeChatLog(), enable_web_search=True
    ) == [{"type": "web_search"}]


@pytest.mark.asyncio
async def test_codex_stream_captures_citations_without_streaming_urls(conversation_module):
    async def stream():
        yield CodexTextDelta("IANA maintains the reserved domains.")
        citation = CodexCitation(
            title="IANA [Reserved] Domains",
            url="https://www.iana.org/help/example-domains",
            start_index=0,
            end_index=4,
        )
        yield CodexCitationDelta(citation)
        yield CodexCitationDelta(citation)
        yield CodexCitationDelta(
            CodexCitation(
                title="Unsafe",
                url="javascript:alert(1)",
                start_index=0,
                end_index=4,
            )
        )

    captured = []
    deltas = [
        delta
        async for delta in conversation_module._codex_stream_to_assistant_deltas(
            stream(),
            citation_sink=captured,
        )
    ]

    assert deltas == [
        {"role": "assistant"},
        {"content": "IANA maintains the reserved domains."},
    ]
    assert captured == [
        CodexCitation(
            title="IANA \\[Reserved\\] Domains",
            url="https://www.iana.org/help/example-domains",
            start_index=0,
            end_index=4,
        )
    ]


def test_citation_result_keeps_sources_in_card_without_changing_speech(conversation_module):
    class Response:
        def __init__(self):
            self.speech = {"plain": {"speech": "IANA maintains the reserved domains."}}
            self.card = {}

        def async_set_card(self, title, content):
            self.card["simple"] = {"title": title, "content": content}

    result = type("Result", (), {"response": Response()})()
    citations = [
        CodexCitation(
            title="IANA",
            url="https://www.iana.org/help/example-domains",
            start_index=0,
            end_index=4,
        )
    ]

    conversation_module._attach_citations_card(result, citations)

    assert result.response.speech["plain"]["speech"] == (
        "IANA maintains the reserved domains."
    )
    assert result.response.card["simple"] == {
        "title": "Sources",
        "content": "- IANA — <https://www.iana.org/help/example-domains>",
    }


def test_web_search_instructions_suppress_spoken_source_urls(conversation_module):
    chat_log = FakeChatLog([FakeContent(role="system", content="Be concise.")])

    assert (
        conversation_module._instructions_for_turn(
            chat_log, "fallback", web_search=False
        )
        == "Be concise."
    )
    instructions = conversation_module._instructions_for_turn(
        chat_log, "fallback", web_search=True
    )

    assert instructions.startswith("Be concise.\n\n")
    assert "do not include raw URLs" in instructions
    assert "renders structured citations separately" in instructions


@pytest.mark.asyncio
async def test_stream_codex_turn_into_chat_log_calls_chat_log_stream_api(
    conversation_module,
):
    chat_log = FakeChatLog()
    codex = FakeCodex([CodexTextDelta("Done")])

    tool_requested = await conversation_module._stream_codex_turn_into_chat_log(
        chat_log=chat_log,
        codex=codex,
        entity_id="conversation.codex_assist",
        model="gpt-5.4",
        instructions="Be concise.",
        input_items=[{"role": "user", "content": "ping"}],
        tools=[],
        reasoning_effort="low",
        reasoning_summary="auto",
        text_verbosity="medium",
    )

    assert tool_requested is False
    assert chat_log.streamed_entity_id == "conversation.codex_assist"
    assert chat_log.streamed_deltas == [{"role": "assistant"}, {"content": "Done"}]
    assert codex.calls == [
        {
            "model": "gpt-5.4",
            "instructions": "Be concise.",
            "input_items": [{"role": "user", "content": "ping"}],
            "tools": [],
            "reasoning_effort": "low",
            "reasoning_summary": "auto",
            "text_verbosity": "medium",
            "text_format": None,
        }
    ]


@pytest.mark.asyncio
async def test_codex_input_from_chat_log_translates_image_attachments(
    conversation_module,
    tmp_path: Path,
):
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"fake-image")
    attachment = type(
        "Attachment",
        (),
        {"mime_type": "image/png", "path": image_path},
    )()
    chat_log = FakeChatLog(
        [FakeContent(role="user", content="describe this", attachments=[attachment])]
    )

    hass = FakeHass()

    result = await conversation_module._codex_input_from_chat_log(hass, chat_log)

    content = result[0]["content"]
    assert content[0] == {"type": "input_text", "text": "describe this"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert hass.executor_jobs[0][0] is conversation_module._image_attachments_for_codex


def test_image_attachments_rejects_too_many_images(
    conversation_module,
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(conversation_module, "MAX_IMAGE_ATTACHMENTS", 2)
    attachments = []
    for index in range(3):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"image")
        attachments.append(type("Attachment", (), {"mime_type": "image/png", "path": path})())

    with pytest.raises(ValueError, match="at most 2 image attachments"):
        conversation_module._image_attachments_for_codex(attachments)


def test_image_attachments_rejects_aggregate_byte_limit(
    conversation_module,
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(conversation_module, "MAX_TOTAL_IMAGE_ATTACHMENT_BYTES", 5)
    attachments = []
    for index in range(2):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"abc")
        attachments.append(type("Attachment", (), {"mime_type": "image/png", "path": path})())

    with pytest.raises(ValueError, match="total attachment size"):
        conversation_module._image_attachments_for_codex(attachments)


def test_image_attachment_growth_is_read_with_a_hard_bound(
    conversation_module,
    monkeypatch,
):
    import io

    monkeypatch.setattr(conversation_module, "MAX_IMAGE_ATTACHMENT_BYTES", 5)
    monkeypatch.setattr(conversation_module, "MAX_TOTAL_IMAGE_ATTACHMENT_BYTES", 5)

    class BoundedReader(io.BytesIO):
        requested_size: int | None = None

        def read(self, size: int | None = -1):
            self.requested_size = size
            return super().read(size)

    reader = BoundedReader(b"unexpectedly large")

    class GrowingPath:
        def stat(self):
            return type("Stat", (), {"st_size": 1})()

        def open(self, mode):
            assert mode == "rb"
            return reader

    attachment = type(
        "Attachment",
        (),
        {"mime_type": "image/png", "path": GrowingPath()},
    )()

    with pytest.raises(ValueError, match="per-file size limit"):
        conversation_module._image_attachments_for_codex([attachment])

    assert reader.requested_size == 6


def test_trim_codex_input_items_drops_orphaned_tool_outputs(conversation_module):
    input_items = [
        {
            "type": "function_call",
            "name": "OldTool",
            "arguments": "{}",
            "call_id": "old-call",
        },
        {
            "type": "function_call_output",
            "call_id": "old-call",
            "output": "{}",
        },
        {"role": "user", "content": "latest"},
        {
            "type": "function_call",
            "name": "NewTool",
            "arguments": "{}",
            "call_id": "new-call",
        },
        {
            "type": "function_call_output",
            "call_id": "new-call",
            "output": "{}",
        },
    ]

    result = conversation_module._trim_codex_input_items(input_items, max_items=4)

    assert result == [
        {"role": "user", "content": "latest"},
        {
            "type": "function_call",
            "name": "NewTool",
            "arguments": "{}",
            "call_id": "new-call",
        },
        {
            "type": "function_call_output",
            "call_id": "new-call",
            "output": "{}",
        },
    ]


def test_trim_codex_input_items_leaves_short_history_unchanged(conversation_module):
    input_items = [{"role": "user", "content": "hello"}]

    assert conversation_module._trim_codex_input_items(input_items, max_items=24) is input_items


def test_trim_codex_input_items_keeps_pair_at_retained_boundary(conversation_module):
    input_items = [
        {"role": "user", "content": "old"},
        {
            "type": "function_call",
            "name": "BoundaryTool",
            "arguments": "{}",
            "call_id": "boundary-call",
        },
        {
            "type": "function_call_output",
            "call_id": "boundary-call",
            "output": "{}",
        },
        {"role": "assistant", "content": "Done."},
    ]

    assert conversation_module._trim_codex_input_items(input_items, max_items=3) == [
        {
            "type": "function_call",
            "name": "BoundaryTool",
            "arguments": "{}",
            "call_id": "boundary-call",
        },
        {
            "type": "function_call_output",
            "call_id": "boundary-call",
            "output": "{}",
        },
        {"role": "assistant", "content": "Done."},
    ]


@pytest.mark.asyncio
async def test_five_tool_rounds_force_one_tools_disabled_final_synthesis(conversation_module):
    calls = []

    async def run_iteration(round_number, allow_tools):
        calls.append((round_number, allow_tools))
        return allow_tools

    await conversation_module._run_tool_rounds(
        max_tool_rounds=5,
        run_iteration=run_iteration,
    )

    assert calls == [
        (1, True),
        (2, True),
        (3, True),
        (4, True),
        (5, True),
        (6, False),
    ]


@pytest.mark.asyncio
async def test_tools_disabled_synthesis_fails_on_an_unexpected_tool_call(conversation_module):
    chat_log = FakeChatLog()
    codex = FakeCodex(
        [
            CodexToolCallDelta(
                CodexToolCall(id="call-1", name="HassTurnOn", arguments={})
            )
        ]
    )

    with pytest.raises(RuntimeError, match="tools are disabled"):
        await conversation_module._stream_codex_turn_into_chat_log(
            chat_log=chat_log,
            codex=codex,
            entity_id="conversation.codex_assist",
            model="gpt-5.4",
            instructions="Give the final answer.",
            input_items=[],
            tools=[],
            reasoning_effort="low",
            reasoning_summary="auto",
            text_verbosity="medium",
            allow_tools=False,
        )
