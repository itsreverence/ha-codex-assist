from __future__ import annotations

import base64
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .codex_auth import CodexTokenSet

ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120


class RuntimeAuthClient(Protocol):
    async def refresh(self, tokens: CodexTokenSet) -> CodexTokenSet: ...


async def resolve_runtime_tokens(
    entry_data: Mapping[str, Any],
    *,
    auth_client: RuntimeAuthClient,
    async_update_entry_data: Callable[[dict[str, Any]], Awaitable[None] | None],
    now: float | None = None,
    refresh_skew_seconds: int = ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> CodexTokenSet:
    access_token = str(entry_data.get("access_token") or "").strip()
    refresh_token = str(entry_data.get("refresh_token") or "").strip()
    if not access_token:
        raise RuntimeError("Codex Assist is missing access_token")

    tokens = CodexTokenSet(access_token=access_token, refresh_token=refresh_token)
    if not access_token_is_expiring(
        access_token,
        now=time.time() if now is None else now,
        skew_seconds=refresh_skew_seconds,
    ):
        return tokens

    if not refresh_token:
        raise RuntimeError("Codex Assist is missing refresh_token")

    refreshed = await auth_client.refresh(tokens)
    updated_data = dict(entry_data)
    updated_data["access_token"] = refreshed.access_token
    updated_data["refresh_token"] = refreshed.refresh_token
    result = async_update_entry_data(updated_data)
    if inspect.isawaitable(result):
        await result
    return refreshed


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
