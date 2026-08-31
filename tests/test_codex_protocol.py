import copy
from dataclasses import asdict

from custom_components.codex_assist.codex_protocol import (
    CodexNativeState,
    native_state_from_response_items,
)


def test_native_state_accepts_only_typed_response_items_and_owns_a_copy():
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Done."}],
    }

    state = native_state_from_response_items([message, {}, {"type": ""}])
    message["content"][0]["text"] = "mutated"

    assert state is not None
    assert state == CodexNativeState(
        (
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        )
    )
    assert repr(state) == "CodexNativeState(item_count=1)"
    assert asdict(state) == {
        "_items": {
            "item_count": 1,
            "redacted": True,
        }
    }
    copied = copy.deepcopy(state)
    assert copied == state
    assert copied is not state

    returned = state.items
    returned[0]["content"][0]["text"] = "also mutated"
    assert state.items[0]["content"][0]["text"] == "Done."


def test_native_state_returns_none_without_replayable_items():
    assert native_state_from_response_items([{}, {"type": ""}]) is None


def test_native_state_requires_completed_assistant_output():
    assert (
        native_state_from_response_items(
            [{"type": "reasoning", "encrypted_content": "encrypted-state"}]
        )
        is None
    )
