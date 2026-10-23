from __future__ import annotations

import asyncio
import base64
import inspect
import json
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .codex_auth import CodexAuthTemporaryError, CodexReauthRequiredError, CodexTokenSet
from .codex_models import ModelDiscoveryCache

ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120


class RuntimeAuthClient(Protocol):
    async def refresh(self, tokens: CodexTokenSet) -> CodexTokenSet: ...


class RuntimeTokenCoordinator:
    """Serialize rotating-token refreshes for one config entry."""

    def __init__(self) -> None:
        self._refresh_lock = asyncio.Lock()
        self.model_cache = ModelDiscoveryCache()

    async def resolve(
        self,
        get_entry_data: Callable[[], Mapping[str, Any]],
        *,
        auth_client: RuntimeAuthClient,
        async_update_entry_data: Callable[[dict[str, Any]], Awaitable[None] | None],
        now: float | None = None,
        refresh_skew_seconds: int = ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    ) -> CodexTokenSet:
        """Resolve current credentials, refreshing once under the entry lock."""
        async with self._refresh_lock:
            return await resolve_runtime_tokens(
                get_entry_data(),
                auth_client=auth_client,
                async_update_entry_data=async_update_entry_data,
                get_current_entry_data=get_entry_data,
                now=now,
                refresh_skew_seconds=refresh_skew_seconds,
            )

    async def refresh_after_rejection(
        self,
        get_entry_data: Callable[[], Mapping[str, Any]],
        *,
        rejected_tokens: CodexTokenSet,
        auth_client: RuntimeAuthClient,
        async_update_entry_data: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> CodexTokenSet:
        """Refresh a rejected token or reuse credentials rotated by another request."""
        async with self._refresh_lock:
            entry_data = get_entry_data()
            current = _tokens_from_entry_data(entry_data)
            if current != rejected_tokens:
                return current
            if not current.refresh_token:
                raise CodexReauthRequiredError("Codex Assist is missing refresh_token")
            refreshed = await auth_client.refresh(current)
            latest_data = get_entry_data()
            latest = _tokens_from_entry_data(latest_data)
            if latest != current:
                return latest
            await _persist_runtime_tokens(latest_data, refreshed, async_update_entry_data)
            return refreshed


_COORDINATORS: weakref.WeakKeyDictionary[Any, RuntimeTokenCoordinator] = (
    weakref.WeakKeyDictionary()
)


def runtime_token_coordinator(entry: Any) -> RuntimeTokenCoordinator:
    """Return the coordinator shared by all platforms for a config entry.

    It outlives reloads. Home Assistant drops ``runtime_data`` on unload while
    requests that hold the previous lock can still be refreshing the token.
    """
    coordinator = _COORDINATORS.get(entry)
    if coordinator is None:
        coordinator = _COORDINATORS[entry] = RuntimeTokenCoordinator()
    return coordinator


async def resolve_runtime_tokens(
    entry_data: Mapping[str, Any],
    *,
    auth_client: RuntimeAuthClient,
    async_update_entry_data: Callable[[dict[str, Any]], Awaitable[None] | None],
    get_current_entry_data: Callable[[], Mapping[str, Any]] | None = None,
    now: float | None = None,
    refresh_skew_seconds: int = ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> CodexTokenSet:
    tokens = _tokens_from_entry_data(entry_data)
    access_token = tokens.access_token
    refresh_token = tokens.refresh_token
    now = time.time() if now is None else now
    exp = _decode_jwt_exp(access_token)
    if exp is None or exp > now + refresh_skew_seconds:
        return tokens

    if not refresh_token:
        raise CodexReauthRequiredError("Codex Assist is missing refresh_token")

    try:
        refreshed = await auth_client.refresh(tokens)
    except CodexAuthTemporaryError:
        if exp > now:
            return tokens
        raise

    latest_data = get_current_entry_data() if get_current_entry_data else entry_data
    latest = _tokens_from_entry_data(latest_data)
    if latest != tokens:
        return latest
    await _persist_runtime_tokens(latest_data, refreshed, async_update_entry_data)
    return refreshed


def _tokens_from_entry_data(entry_data: Mapping[str, Any]) -> CodexTokenSet:
    access_token = str(entry_data.get("access_token") or "").strip()
    refresh_token = str(entry_data.get("refresh_token") or "").strip()
    if not access_token:
        raise CodexReauthRequiredError("Codex Assist is missing access_token")
    return CodexTokenSet(access_token=access_token, refresh_token=refresh_token)


async def _persist_runtime_tokens(
    entry_data: Mapping[str, Any],
    tokens: CodexTokenSet,
    async_update_entry_data: Callable[[dict[str, Any]], Awaitable[None] | None],
) -> None:
    updated_data = dict(entry_data)
    updated_data["access_token"] = tokens.access_token
    updated_data["refresh_token"] = tokens.refresh_token
    result = async_update_entry_data(updated_data)
    if inspect.isawaitable(result):
        await result


def access_token_is_expiring(
    access_token: str,
    *,
    now: float,
    skew_seconds: int = ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> bool:
    exp = _decode_jwt_exp(access_token)
    if exp is None:
        return False
    return exp <= now + skew_seconds


def _decode_jwt_exp(access_token: str) -> float | None:
    parts = access_token.split(".")
    if len(parts) < 2:
        return None
    payload_segment = parts[1]
    padding = "=" * (-len(payload_segment) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode((payload_segment + padding).encode())
        payload = json.loads(payload_bytes.decode())
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return float(exp)
