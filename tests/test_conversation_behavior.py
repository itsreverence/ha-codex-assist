from __future__ import annotations

import importlib
import json
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.codex_assist.codex_client import (
    CodexAuthenticationError,
    CodexResponseItemDelta,
    CodexStreamDelta,
    CodexTextDelta,
    CodexToolCall,
    CodexToolCallDelta,
)
from custom_components.codex_assist.codex_protocol import CodexNativeState
from tests.ha_fakes import install_homeassistant_fakes


@dataclass
class FakeContent:
    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_result: dict | None = None
    tool_calls: list | None = None
    attachments: list | None = None
    native: object | None = None


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


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "", " \n"])
async def test_silent_completed_stream_rejects_native_state(
    conversation_module, text,
):
    async def stream():
        if text is not None:
            yield CodexTextDelta(text)
        yield CodexResponseItemDelta({"type": "reasoning", "encrypted_content": "private"})

    delivered = []
    with pytest.raises(RuntimeError, match="^Codex returned no answer text$"):
        async for item in conversation_module._codex_stream_to_assistant_deltas(
            stream(),
        ):
            delivered.append(item)
    assert not any("native" in item for item in delivered)


@pytest.mark.asyncio
async def test_failed_auth_attempt_discards_native_state_before_visible_output(conversation_module):
    async def stream():
        yield CodexResponseItemDelta({"type": "reasoning", "encrypted_content": "private"})
        raise CodexAuthenticationError("Synthetic rejection")

    delivered = []
    with pytest.raises(CodexAuthenticationError) as raised:
        async for item in conversation_module._codex_stream_to_assistant_deltas(
            stream(),
        ):
            delivered.append(item)
    assert raised.value.retry_allowed
    assert delivered == []


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


def test_codex_tools_pass_home_country_to_web_search(conversation_module):
    chat_log = FakeChatLog()
    chat_log.hass = type("Hass", (), {"config": type("Config", (), {"country": "US"})()})()

    assert conversation_module._codex_tools_from_chat_log(chat_log, enable_web_search=True) == [
        {"type": "web_search", "user_location": {"type": "approximate", "country": "US"}}
    ]


def test_web_search_instructions_suppress_spoken_source_urls(conversation_module):
    chat_log = FakeChatLog([FakeContent(role="system", content="Be concise.")])

    without_search = conversation_module._instructions_for_turn(
        chat_log, "fallback", web_search=False
    )
    assert without_search.startswith("Be concise.\n\n")
    assert "Keep error responses short" in without_search
    assert "When clarification is required" in without_search
    assert "do not include raw URLs" not in without_search
    instructions = conversation_module._instructions_for_turn(
        chat_log, "fallback", web_search=True
    )

    assert instructions.startswith("Be concise.\n\n")
    assert "do not include raw URLs" in instructions
    assert "markdown links" in instructions
    assert "Source/Sources section" in instructions
    assert "human-readable names only" in instructions
    assert "renders structured citations separately" not in instructions


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


@pytest.mark.parametrize(
    "failure", ["missing_path", "missing_file", "stat", "open", "read", "size"]
)
@pytest.mark.parametrize("with_valid_image", [False, True])
def test_image_attachments_reject_unusable_image_without_private_paths(
    conversation_module, monkeypatch, tmp_path, caplog, failure, with_valid_image
):
    marker = "synthetic-private-attachment"
    path = tmp_path / f"{marker}.png"
    path.write_bytes(b"123456")
    monkeypatch.setattr(conversation_module, "MAX_IMAGE_ATTACHMENT_BYTES", 5)
    attachment = SimpleNamespace(mime_type="image/png", path=path)
    expected = "Codex Assist could not read an image attachment"
    if failure == "missing_path":
        attachment = SimpleNamespace(mime_type="image/png")
    elif failure == "missing_file":
        path.unlink()
    elif failure in {"stat", "open", "read"}:
        error = PermissionError(13, marker, str(path))
        fake_path = MagicMock()
        fake_path.stat.return_value.st_size = 3
        if failure == "read":
            fake_path.open.return_value.__enter__.return_value.read.side_effect = error
        else:
            getattr(fake_path, failure).side_effect = error
        attachment.path = fake_path
    else:
        expected = "Codex Assist image attachment exceeds the 10 MiB per-file limit"

    attachments = [attachment]
    if with_valid_image:
        valid_path = tmp_path / "valid.png"
        valid_path.write_bytes(b"valid")
        attachments.insert(0, SimpleNamespace(mime_type="image/png", path=valid_path))

    with pytest.raises(ValueError) as caught:
        try:
            conversation_module._image_attachments_for_codex(attachments)
        except ValueError:
            logging.getLogger(__name__).exception("Attachment conversion failed")
            raise

    assert str(caught.value) == expected
    assert marker not in "".join(traceback.format_exception(caught.value))
    assert marker not in caplog.text
    assert "Attachment conversion failed" in caplog.text


def test_image_attachments_accept_exact_file_count_and_total_limits(
    conversation_module, monkeypatch, tmp_path
):
    monkeypatch.setattr(conversation_module, "MAX_IMAGE_ATTACHMENT_BYTES", 5)
    monkeypatch.setattr(conversation_module, "MAX_TOTAL_IMAGE_ATTACHMENT_BYTES", 20)
    path = tmp_path / "image.png"
    path.write_bytes(b"12345")
    attachment = SimpleNamespace(mime_type="image/png", path=path)

    assert conversation_module._image_attachments_for_codex([attachment] * 4) == [
        ("image/png", b"12345")
    ] * 4


@pytest.mark.parametrize("mime_type", ["text/plain", None, "", 123])
def test_image_attachments_still_ignore_non_images(conversation_module, mime_type):
    attachment = SimpleNamespace(mime_type=mime_type)

    assert conversation_module._image_attachments_for_codex([attachment]) == []


def test_image_attachments_rejects_too_many_images(
    conversation_module,
    tmp_path: Path,
):
    attachments = []
    for index in range(5):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"image")
        attachments.append(type("Attachment", (), {"mime_type": "image/png", "path": path})())

    with pytest.raises(ValueError, match="at most 4 image attachments"):
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

    assert conversation_module._trim_codex_input_items(input_items, max_items=24) == [
        {"role": "user", "content": "hello"}
    ]


def test_trim_codex_input_items_keeps_complete_turn_at_retained_boundary(
    conversation_module,
):
    input_items = [
        {"role": "user", "content": "discarded"},
        {"role": "user", "content": "retained"},
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

    assert conversation_module._trim_codex_input_items(input_items, max_items=4) == [
        {"role": "user", "content": "retained"},
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


def test_trim_codex_input_items_rejects_oversize_current_turn(conversation_module):
    input_items = [
        {"role": "user", "content": "current"},
        *[
            {"type": "reasoning", "id": f"rs_{index}"}
            for index in range(3)
        ],
    ]

    with pytest.raises(ValueError, match="contains 4 items; maximum is 3"):
        conversation_module._trim_codex_input_items(input_items, max_items=3)


@pytest.mark.asyncio
async def test_codex_input_replays_owned_native_items_without_reconstruction(
    conversation_module,
):
    native_items = (
        {
            "id": "rs_1",
            "type": "reasoning",
            "encrypted_content": "encrypted-state",
            "summary": [],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "HassTurnOn",
            "arguments": '{"name":"Kitchen"}',
            "status": "completed",
        },
    )
    chat_log = FakeChatLog(
        [
            FakeContent(role="user", content="turn on kitchen"),
            FakeContent(
                role="assistant",
                content="duplicate visible text must not be replayed",
                tool_calls=[FakeToolCall("call-1", "HassTurnOn", {"name": "Kitchen"})],
                native=CodexNativeState(native_items),
            ),
            FakeContent(
                role="tool_result",
                tool_call_id="call-1",
                tool_result={"success": True},
            ),
        ]
    )

    result = await conversation_module._codex_input_from_chat_log(object(), chat_log)

    assert result == [
        {"role": "user", "content": "turn on kitchen"},
        *native_items,
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": json.dumps({"success": True}),
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("search_count", [4, 8])
async def test_search_native_history_keeps_complete_pairs_at_24_item_trim(
    conversation_module, search_count,
):
    """Trim whole user turns, never amputate reasoning or search call/output pairs."""
    def search_history(prefix, count):
        items = []
        for index in range(count):
            call_id = f"{prefix}-{index}"
            items.extend([
                {"type": "reasoning", "encrypted_content": f"reasoning-{call_id}"},
                {
                    "type": "function_call", "name": "web_search", "call_id": call_id,
                    "arguments": '{"queries":["synthetic"]}',
                },
                {
                    "type": "function_call_output", "call_id": call_id,
                    "output": f"Fact {call_id} https://example.com/{call_id}",
                },
            ])
        items.append({"type": "message", "role": "assistant", "content": []})
        return items

    old_native = search_history("old", 4)
    newest_native = search_history("new", search_count)
    chat_log = FakeChatLog([
        FakeContent(role="user", content="old request"),
        FakeContent(role="assistant", native=CodexNativeState(old_native)),
        FakeContent(role="user", content="new request"),
        FakeContent(role="assistant", native=CodexNativeState(newest_native)),
    ])
    if search_count == 8:
        with pytest.raises(ValueError, match="contains 26 items; maximum is 24"):
            await conversation_module._codex_input_from_chat_log(object(), chat_log)
        return
    replay = await conversation_module._codex_input_from_chat_log(object(), chat_log)
    assert replay == [{"role": "user", "content": "new request"}, *newest_native]
    assert len(replay) <= 24
    assert [item["call_id"] for item in replay if item.get("type") == "function_call"] == [
        item["call_id"] for item in replay if item.get("type") == "function_call_output"
    ]


@pytest.mark.asyncio
async def test_codex_input_ignores_unowned_native_state(conversation_module):
    content = FakeContent(
        role="assistant",
        content="safe visible fallback",
        native={"items": [{"type": "reasoning", "encrypted_content": "injected"}]},
    )

    result = await conversation_module._codex_input_from_chat_log(
        object(), FakeChatLog([content])
    )

    assert result == [{"role": "assistant", "content": "safe visible fallback"}]


@pytest.mark.asyncio
async def test_codex_stream_attaches_native_state_outside_visible_deltas(
    conversation_module,
):
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "encrypted_content": "encrypted-state",
        "summary": [],
    }
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "Done.", "annotations": []}],
    }

    async def stream():
        yield CodexTextDelta("Done.")
        yield CodexResponseItemDelta(reasoning)
        yield CodexResponseItemDelta(message)

    deltas = [
        delta
        async for delta in conversation_module._codex_stream_to_assistant_deltas(stream())
    ]

    assert deltas[:2] == [{"role": "assistant"}, {"content": "Done."}]
    assert deltas[2] == {"native": CodexNativeState((reasoning, message))}
    assert "encrypted-state" not in str(deltas[:2])


@pytest.mark.asyncio
async def test_codex_stream_does_not_attach_incomplete_reasoning_state(
    conversation_module,
):
    async def stream():
        yield CodexTextDelta("Visible fallback.")
        yield CodexResponseItemDelta(
            {
                "id": "rs_1",
                "type": "reasoning",
                "encrypted_content": "incomplete-state",
            }
        )

    assert [
        delta
        async for delta in conversation_module._codex_stream_to_assistant_deltas(stream())
    ] == [
        {"role": "assistant"},
        {"content": "Visible fallback."},
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
async def test_native_transcript_survives_tool_round_and_final_synthesis(
    conversation_module,
):
    chat_log = FakeChatLog([FakeContent(role="user", content="Complete the task")])
    requests: list[tuple[bool, list[dict]]] = []
    tool_items = (
        {"id": "rs-tool", "type": "reasoning", "encrypted_content": "tool-state"},
        {
            "id": "fc-tool",
            "type": "function_call",
            "call_id": "call-1",
            "name": "HassTurnOn",
            "arguments": '{"name":"Kitchen"}',
        },
    )
    final_items = (
        {"id": "rs-final", "type": "reasoning", "encrypted_content": "final-state"},
        {"id": "msg-final", "type": "message", "role": "assistant", "content": []},
    )

    async def run_iteration(_round_number, allow_tools):
        input_items = await conversation_module._codex_input_from_chat_log(
            object(), chat_log
        )
        requests.append((allow_tools, input_items))
        native_items = tool_items if allow_tools else final_items
        deltas: list[CodexStreamDelta] = [
            CodexResponseItemDelta(item) for item in native_items
        ]
        if allow_tools:
            deltas.append(
                CodexToolCallDelta(
                    CodexToolCall(
                        id="call-1",
                        name="HassTurnOn",
                        arguments={"name": "Kitchen"},
                    )
                )
            )
        else:
            deltas.insert(0, CodexTextDelta("All done."))

        stream_start = len(chat_log.streamed_deltas)
        tool_requested = await conversation_module._stream_codex_turn_into_chat_log(
            chat_log=chat_log,
            codex=FakeCodex(deltas),
            entity_id="conversation.codex_assist",
            model="gpt-5.4",
            instructions="Complete the task.",
            input_items=input_items,
            tools=[{"type": "function", "name": "HassTurnOn"}] if allow_tools else [],
            reasoning_effort="low",
            reasoning_summary="auto",
            text_verbosity="medium",
            allow_tools=allow_tools,
        )
        round_deltas = chat_log.streamed_deltas[stream_start:]
        native_state = next(delta["native"] for delta in round_deltas if "native" in delta)
        chat_log.content.append(
            FakeContent(
                role="assistant",
                content=None if allow_tools else "All done.",
                tool_calls=(
                    [FakeToolCall("call-1", "HassTurnOn", {"name": "Kitchen"})]
                    if allow_tools
                    else None
                ),
                native=native_state,
            )
        )
        if allow_tools:
            chat_log.content.append(
                FakeContent(
                    role="tool_result",
                    tool_call_id="call-1",
                    tool_result={"success": True},
                )
            )
        return tool_requested

    await conversation_module._run_tool_rounds(
        max_tool_rounds=1,
        run_iteration=run_iteration,
    )

    assert [allow_tools for allow_tools, _input_items in requests] == [True, False]
    assert requests[1][1] == [
        {"role": "user", "content": "Complete the task"},
        *tool_items,
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": json.dumps({"success": True}),
        },
    ]

    chat_log.content.append(FakeContent(role="user", content="What happened?"))
    next_turn_input = await conversation_module._codex_input_from_chat_log(
        object(), chat_log
    )

    assert next_turn_input[-3:] == [
        *final_items,
        {"role": "user", "content": "What happened?"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("delta,visible", [
    (None, False),
    (CodexTextDelta("Partial answer"), True),
    (CodexToolCallDelta(CodexToolCall("call-1", "HassTestReading", {})), True),
    (CodexResponseItemDelta({"type": "reasoning"}), False),
])
async def test_auth_replay_is_disabled_after_visible_deltas(conversation_module, delta, visible):
    async def stream():
        if delta is not None:
            yield delta
        raise CodexAuthenticationError("Synthetic auth rejection")

    with pytest.raises(CodexAuthenticationError) as caught:
        async for _ in conversation_module._codex_stream_to_assistant_deltas(stream()):
            pass
    assert getattr(caught.value, "retry_allowed", True) is (not visible)


@pytest.mark.parametrize("retained", [False, True])
async def test_history_reads_invalid_images_only_in_retained_turns(
    conversation_module, tmp_path, retained
):
    image = SimpleNamespace(mime_type="image/png", path=tmp_path / "missing.png")
    old = FakeContent(role="user", content="Old turn", attachments=[image])
    recent = [FakeContent(role="user", content=f"Recent {i}") for i in range(24)]
    log = FakeChatLog([old] if retained else [old, *recent])
    hass = FakeHass()

    if retained:
        with pytest.raises(ValueError, match="could not read an image attachment"):
            await conversation_module._codex_input_from_chat_log(hass, log)
        assert len(hass.executor_jobs) == 1
    else:
        assert await conversation_module._codex_input_from_chat_log(hass, log) == [
            {"role": "user", "content": f"Recent {i}"} for i in range(24)
        ]
        assert hass.executor_jobs == []


async def test_history_materializes_only_retained_images(conversation_module, tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"image")
    image = SimpleNamespace(mime_type="image/png", path=path)
    log = FakeChatLog([
        FakeContent(role="user", content=f"Turn {i}", attachments=[image])
        for i in range(30)
    ])
    hass = FakeHass()

    result = await conversation_module._codex_input_from_chat_log(hass, log)

    assert result == [
        {"role": "user", "content": [
            {"type": "input_text", "text": f"Turn {i}"},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,aW1hZ2U=",
                "detail": "auto",
            },
        ]}
        for i in range(6, 30)
    ]
    assert len(hass.executor_jobs) == 24
