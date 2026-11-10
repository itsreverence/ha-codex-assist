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
    ([delta("Hel", item_id="a"), delta("lo", item_id="a", content_index=0),
      message("Hello", item_id="a")], ["Hel", "lo"]),
    ([message(item_id="a", output_index=0, event="added"),
      delta("Hel", output_index=0, content_index=0),
      message("Hello", item_id="a")], ["Hel", "lo"]),
    ([message("Hello", output_index=0), message("Hello", output_index=0)], ["Hello"]),
    ([delta("One ", item_id="a", content_index=0),
      delta("two", item_id="a", content_index=1),
      message("One ", "two", item_id="a")], ["One ", "two"]),
    ([message()], []),
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
    [message("One"), message("Two")],
    [message("One"), message("One")],
    [{**message("One", item_id="a"), "item_id": "b"}],
    [delta("Later", item_id="b", output_index=1, content_index=0),
     delta("Earlier", item_id="a", output_index=0, content_index=0),
     message("Later!", item_id="b", output_index=1)],
    [delta("Hel", item_id="a", content_index=0), message("Hello", output_index=0)],
    [delta("Hel", output_index=0, content_index=0), message("Hello", item_id="a")],
    [message("Hello", item_id="a"), message("Hello", output_index=0)],
])
async def test_ambiguous_conflicting_or_out_of_order_recovery_fails(events):
    with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
        await collect([*events, COMPLETED], [])


@pytest.mark.parametrize("event", ["done", "added"])
@pytest.mark.parametrize("late_item", ["a", "b"])
@pytest.mark.parametrize("middle", [[], [message("Middle", item_id="c")]])
async def test_late_index_alias_rejects_reordered_recovered_text(event, late_item, middle):
    delivered = []
    events = [
        message("Later", item_id="b", output_index=None if late_item == "b" else 1),
        *middle,
        message("Earlier", item_id="a", output_index=None if late_item == "a" else 0),
        message(
            "Earlier" if late_item == "a" else "Later", item_id=late_item,
            output_index=0 if late_item == "a" else 1, event=event,
        ),
        COMPLETED,
    ]
    with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
        await collect(events, delivered)
    assert delivered == (["Later", "Middle", "Earlier"] if middle else ["Later", "Earlier"])


@pytest.mark.parametrize("event", ["done", "added"])
@pytest.mark.parametrize("late_item", ["a", "b"])
async def test_valid_late_index_alias_preserves_recovered_text(event, late_item):
    delivered = []
    await collect([
        message("Earlier", item_id="a", output_index=None if late_item == "a" else 0),
        message("Later", item_id="b", output_index=None if late_item == "b" else 1),
        message(
            "Earlier" if late_item == "a" else "Later", item_id=late_item,
            output_index=0 if late_item == "a" else 1, event=event,
        ),
        message("Earlier", item_id="a", output_index=0),
        message("Later", item_id="b", output_index=1),
        COMPLETED,
    ], delivered)
    assert delivered == ["Earlier", "Later"]


async def test_recovered_text_still_requires_response_completion():
    delivered = []
    with pytest.raises(RuntimeError, match="before response.completed"):
        await collect([message("Answer", item_id="a")], delivered)
    assert delivered == ["Answer"]


@pytest.mark.parametrize("binding", ["done", "added"])
async def test_interleaved_deltas_retain_order_until_late_index_binding(binding):
    with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
        await collect([
            delta("L", item_id="b", content_index=0),
            delta("E", item_id="a", output_index=0, content_index=0),
            delta("l", item_id="b", content_index=0),
            message("Ll", item_id="b", output_index=1, event=binding),
            message("E", item_id="a", output_index=0),
            COMPLETED,
        ], [])


@pytest.mark.parametrize("finish_message", [False, True])
async def test_fully_delivered_parts_must_follow_content_order(finish_message):
    events = [
        delta("Later", item_id="a", output_index=0, content_index=1),
        delta("Earlier", item_id="a", output_index=0, content_index=0),
    ]
    if finish_message:
        events.append(message("Earlier", "Later", item_id="a", output_index=0))
    with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
        await collect([*events, COMPLETED], [])


@pytest.mark.parametrize("multiple_parts", [False, True])
async def test_ordered_multichunk_parts_with_late_index_binding(multiple_parts):
    delivered = []
    events = [
        delta("Ear", item_id="a", content_index=0),
        delta("lier", item_id="a", content_index=0),
    ]
    if multiple_parts:
        events.extend([
            delta("La", item_id="a", content_index=1),
            delta("ter", item_id="a", content_index=1),
            message("Earlier", "Later", item_id="a", output_index=0),
        ])
    else:
        events.append(message("Earlier", item_id="a", output_index=0))
    await collect([*events, COMPLETED], delivered)
    assert delivered == (["Ear", "lier", "La", "ter"] if multiple_parts else ["Ear", "lier"])


@pytest.mark.parametrize("a,b", [
    pytest.param({"output_index": 0}, {"output_index": 1}, id="full-index"),
    pytest.param({"output_index": 0}, {}, id="only-a-index"),
    pytest.param({}, {"output_index": 1}, id="only-b-index"),
    pytest.param({}, {}, id="no-index"),
])
@pytest.mark.parametrize("shape", ["same-part", "different-parts", "within-message"])
async def test_contiguous_delivery_rejects_message_or_part_resumption(a, b, shape):
    middle_id = "a" if shape == "within-message" else "b"
    middle_index = 1 if shape == "within-message" else 0
    last_index = 1 if shape == "different-parts" else 0
    delivered = []
    with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
        await collect([
            delta("E", item_id="a", content_index=0, **a),
            delta("X", item_id=middle_id, content_index=middle_index,
                  **(a if middle_id == "a" else b)),
            delta("e", item_id="a", content_index=last_index, **a),
            message(*(["E", "e"] if last_index else ["Ee"]), item_id="a", **a),
            message("X", item_id="b", **b),
            COMPLETED,
        ], delivered)
    assert delivered == ["E", "X"]


@pytest.mark.parametrize("a,b", [
    pytest.param({"output_index": 0}, {"output_index": 1}, id="full-index"),
    pytest.param({"output_index": 0}, {}, id="only-a-index"),
    pytest.param({}, {"output_index": 1}, id="only-b-index"),
    pytest.param({}, {}, id="no-index"),
])
@pytest.mark.parametrize("last_index", [0, 1], ids=["same-part", "different-parts"])
async def test_contiguous_delivery_preserves_ordered_messages(a, b, last_index):
    delivered = []
    await collect([
        delta("E", item_id="a", content_index=0, **a),
        delta("e", item_id="a", content_index=last_index, **a),
        delta("X", item_id="b", content_index=0, **b),
        message(*(["E", "e"] if last_index else ["Ee"]), item_id="a", **a),
        message("X", item_id="b", **b),
        COMPLETED,
    ], delivered)
    assert delivered == ["E", "e", "X"]


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("binding", ["message-alias", "content-index"])
async def test_contiguous_delivery_resolves_late_identity_before_resumption(binding, resume):
    events = [
        delta("E", **({"output_index": 0, "content_index": 0}
                      if binding == "message-alias" else {"item_id": "a"})),
        delta("X", item_id="b", content_index=0),
    ]
    if binding == "message-alias":
        events.append(message(item_id="a", output_index=0, event="added"))
    if resume:
        events.append(delta("e", item_id="a", content_index=0))
    events.extend([
        message("Ee" if resume else "E", item_id="a"),
        message("X", item_id="b"), COMPLETED,
    ])
    delivered = []
    if resume:
        with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
            await collect(events, delivered)
    else:
        await collect(events, delivered)
    assert delivered == ["E", "X"]


async def test_contiguous_delivery_preserves_long_adjacent_chunks_and_recovered_suffix():
    delivered = []
    await collect([
        *(delta("x", item_id="a") for _ in range(2048)),
        message("x" * 2048 + "!", item_id="a", output_index=0),
        message("Next", item_id="b"), COMPLETED,
    ], delivered)
    assert delivered == [*["x"] * 2048, "!", "Next"]


@pytest.mark.parametrize("identities", [
    ({"item_id": "a"}, {"item_id": "b"}),
    ({"output_index": 0}, {"output_index": 1}),
])
@pytest.mark.parametrize("ending", [
    pytest.param([COMPLETED], id="delta-only-completion"),
    pytest.param([], id="premature-eof"),
    pytest.param([message("Ee", item_id="a"), COMPLETED], id="message-done"),
])
async def test_anonymous_delta_after_multiple_messages_fails_before_delivery(identities, ending):
    first, second = identities
    delivered = []
    with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
        await collect([
            delta("E", **first), delta("X", **second), delta("e"), *ending,
        ], delivered)
    assert delivered == ["E", "X"]


async def test_anonymous_delta_stops_reading_before_long_ambiguous_tail():
    class CountingStream(httpx.AsyncByteStream):
        events_read = 0

        async def __aiter__(self):
            for event in [
                delta("E", item_id="a"), delta("X", item_id="b"),
                *(delta("e") for _ in range(2048)), COMPLETED,
            ]:
                self.events_read += 1
                yield encode([event])

    stream = CountingStream()
    delivered = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream),
    )) as http:
        client = CodexClient(http_client=http, access_token="test")
        with pytest.raises(RuntimeError, match="^Codex returned inconsistent answer text$"):
            async for item in client.stream_turn(model="test", instructions="Test", input_items=[]):
                if isinstance(item, CodexTextDelta):
                    delivered.append(item.text)
    assert delivered == ["E", "X"]
    assert stream.events_read == 3


@pytest.mark.parametrize("events,expected", [
    pytest.param([delta("E", item_id="a"), delta("e")], ["E", "e"],
                 id="single-message-anonymous"),
    pytest.param([delta("E", item_id="a"), delta("X", item_id="b"), delta("x", item_id="b")],
                 ["E", "X", "x"], id="multiple-messages-identified"),
    pytest.param([delta("E", output_index=0), delta("X", output_index=1),
                  delta("x", output_index=1)], ["E", "X", "x"], id="multiple-messages-indexed"),
    pytest.param([delta("E", item_id="a"), delta("X", item_id="b"), delta("")],
                 ["E", "X"], id="multiple-messages-empty-anonymous"),
])
async def test_anonymous_delta_preserves_unambiguous_delivery(events, expected):
    delivered = []
    await collect([*events, COMPLETED], delivered)
    assert delivered == expected
