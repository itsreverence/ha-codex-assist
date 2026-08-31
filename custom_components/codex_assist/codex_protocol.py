from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class _TraceRedactedItems:
    """Own native items while emitting only metadata through dataclass traces."""

    __slots__ = ("_items",)

    def __init__(self, items: Iterable[dict[str, Any]]) -> None:
        self._items = tuple(copy.deepcopy(item) for item in items)

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _TraceRedactedItems):
            return NotImplemented
        return self._items == other._items

    def copy_items(self) -> tuple[dict[str, Any], ...]:
        """Return an isolated copy of the owned provider items."""
        return copy.deepcopy(self._items)

    def __deepcopy__(self, _memo: dict[int, Any]) -> dict[str, int | bool]:
        """Keep dataclasses.asdict traces free of native provider content."""
        return {"item_count": len(self._items), "redacted": True}


@dataclass(frozen=True, repr=False, init=False)
class CodexNativeState:
    """Provider output items captured from one Codex Responses round."""

    _items: _TraceRedactedItems

    def __init__(self, items: Iterable[dict[str, Any]]) -> None:
        object.__setattr__(self, "_items", _TraceRedactedItems(items))

    @property
    def items(self) -> tuple[dict[str, Any], ...]:
        """Return an isolated copy of the provider transcript items."""
        return self._items.copy_items()

    def __repr__(self) -> str:
        """Keep opaque provider state out of Home Assistant debug logs."""
        return f"CodexNativeState(item_count={len(self._items)})"

    def __deepcopy__(self, memo: dict[int, Any]) -> CodexNativeState:
        """Preserve replay state when the native wrapper itself is copied."""
        duplicate = CodexNativeState(self.items)
        memo[id(self)] = duplicate
        return duplicate


def native_state_from_response_items(
    items: Iterable[dict[str, Any]],
) -> CodexNativeState | None:
    """Own replayable typed Responses items without trusting outside mutation."""
    accepted = tuple(
        item for item in items if isinstance(item.get("type"), str) and item["type"]
    )
    has_assistant_output = any(
        item.get("type") in {"function_call", "message"} for item in accepted
    )
    return CodexNativeState(accepted) if has_assistant_output else None
