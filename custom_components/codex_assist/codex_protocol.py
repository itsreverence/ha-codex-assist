from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, repr=False)
class CodexNativeState:
    """Provider output items captured from one Codex Responses round."""

    _items: tuple[dict[str, Any], ...]

    @property
    def items(self) -> tuple[dict[str, Any], ...]:
        """Return an isolated copy of the provider transcript items."""
        return copy.deepcopy(self._items)

    def __repr__(self) -> str:
        """Keep opaque provider state out of Home Assistant debug logs."""
        return f"CodexNativeState(item_count={len(self._items)})"


def native_state_from_response_items(
    items: Iterable[dict[str, Any]],
) -> CodexNativeState | None:
    """Own replayable typed Responses items without trusting outside mutation."""
    accepted = tuple(
        copy.deepcopy(item)
        for item in items
        if isinstance(item.get("type"), str) and item["type"]
    )
    has_assistant_output = any(
        item.get("type") in {"function_call", "message"} for item in accepted
    )
    return CodexNativeState(accepted) if has_assistant_output else None
