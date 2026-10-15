"""Completed message text reconciles with the text already delivered over SSE."""

import httpx
import pytest

from custom_components.codex_assist.codex_client import CodexClient, CodexTextDelta
from tests.test_codex_stream_completion import COMPLETED, Stream, encode


def delta(text, **identity):
    return {"type": "response.output_text.delta", "delta": text, **identity}


def message(*parts, item_id=None, output_index=None, event="done", **metadata):
    item = {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": text} for text in parts], **metadata,
    }
    if item_id is not None:
        item["id"] = item_id
    result = {"type": f"response.output_item.{event}", "item": item}
    if output_index is not None:
        result["output_index"] = output_index
    return result


async def collect(events, delivered):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Stream(encode(events))),
    )) as http:
        client = CodexClient(http_client=http, access_token="test")
        async for item in client.stream_turn(model="test", instructions="Test", input_items=[]):
            if isinstance(item, CodexTextDelta):
                delivered.append(item.text)


@pytest.mark.parametrize("events,expected", [
    ([message("  Hello\n", item_id="a")], ["  Hello\n"]),
    ([delta("The fi"), message("The final answer.")], ["The fi", "nal answer."]),
    ([delta("Hello"), message("Hello")], ["Hello"]),
    ([message("Yes.", item_id="a"), message("Yes.", item_id="b")], ["Yes.", "Yes."]),
    ([delta("Hel", output_index=0, content_index=0),
      message("Hello", item_id="a", output_index=0)], ["Hel", "lo"]),
    ([message(item_id="a", output_index=0, event="added"),
      delta("Hel", item_id="a", content_index=0),
      message("Hello", output_index=0)], ["Hel", "lo"]),
    ([message("Hello", item_id="a"), message("Hello", item_id="a")], ["Hello"]),
    ([message("One ", "two", item_id="a")], ["One ", "two"]),
    ([delta("One ", item_id="a", content_index=0),
      message("One ", "two", item_id="a")], ["One ", "two"]),
    ([delta("Hel", item_id="a"), message("Hello", item_id="a")], ["Hel", "lo"]),
    ([delta("Hel", item_id="a", content_index=0), delta("lo", item_id="a"),
      message("Hello", item_id="a")], ["Hel", "lo"]),
    ([message("Hidden", item_id="a", phase="commentary")], []),
    ([message("Hidden", item_id="a", role="user")], []),
    ([message(item_id="a", event="added", phase="commentary"),
      message("Hidden", item_id="a")], []),
    ([{"type": "response.completed", "response": {
        "output": [message("No snapshot recovery")["item"]], "status": "completed",
    }}], []),
])
async def test_completed_text_recovers_only_missing_suffix(events, expected):
    delivered = []
    await collect([*events, COMPLETED], delivered)
    assert delivered == expected


@pytest.mark.parametrize("events", [
    [delta("Wrong", item_id="a", content_index=0), message("Hello", item_id="a")],
    [message("Hello", item_id="a"), message("Different", item_id="a")],
    [delta("two", item_id="a", content_index=1), message("One ", "two", item_id="a")],
    [delta("O", item_id="a", content_index=0), delta("two", item_id="a", content_index=1),
     message("One ", "two", item_id="a")],
    [message(item_id="a", output_index=0, event="added"),
     delta("Later", item_id="b", output_index=1, content_index=0),
     message("Earlier", item_id="a", output_index=0)],
    [delta("Later", item_id="b", output_index=1, content_index=0),
     message("Earlier", item_id="a", output_index=0)],
    [delta("Hel", item_id="a", output_index=0),
     message("Hello", item_id="b", output_index=0)],
    [delta("One", item_id="a"), delta("Two", output_index=0),
     message("OneTwo", item_id="a", output_index=0)],
    [delta("One"), message("One", "two", item_id="a")],
    [delta("One", item_id="a"), message("One", "two", item_id="a")],
    [delta("One"), message("One", item_id="a"), message("Two", item_id="b")],
    [delta("One", item_id="a", content_index=0),
     delta("Two", item_id="b", content_index=0), message("One!", item_id="a")],
])
async def test_ambiguous_conflicting_or_out_of_order_recovery_fails(events):
    with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
        await collect([*events, COMPLETED], [])


async def test_recovered_text_still_requires_response_completion():
    delivered = []
    with pytest.raises(RuntimeError, match="before response.completed"):
        await collect([message("Answer", item_id="a")], delivered)
    assert delivered == ["Answer"]
