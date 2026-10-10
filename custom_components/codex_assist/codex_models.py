from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

import httpx

from .codex_client import codex_headers

CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
# Only used when this session has no successful account discovery result.
# Reviewed 2026-10-10 against https://learn.chatgpt.com/docs/models.
# See docs/RELEASING.md before updating these unverified suggestions.
DEFAULT_CODEX_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
MODEL_CACHE_SECONDS = 6 * 60 * 60
MODEL_RETRY_SECONDS = 60
MODEL_REFRESH_DEBOUNCE_SECONDS = 30


class ModelDiscoveryError(Exception):
    """A sanitized discovery failure; never includes response bodies or credentials."""

    def __init__(self, reason: str = "unavailable") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ModelCatalog:
    models: tuple[str, ...]
    source: Literal["discovered", "cached", "fallback"]
    error: str | None = None


class ModelDiscoveryCache:
    """One entry's in-memory model IDs, with bounded refresh and retry frequency."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._generation = 0
        self.clear()

    def clear(self) -> None:
        """Discard account data when device-code sign-in replaces credentials."""
        self._generation += 1
        self._models: tuple[str, ...] | None = None
        self._last_attempt = -math.inf
        self._last_success = -math.inf
        self._error: str | None = None

    def _snapshot(self) -> ModelCatalog:
        if self._models is None:
            return ModelCatalog(tuple(DEFAULT_CODEX_MODELS), "fallback", self._error)
        return ModelCatalog(self._models, "cached", self._error)

    async def async_get(
        self, fetch: Callable[[], Awaitable[list[str]]], *, force: bool = False
    ) -> ModelCatalog:
        async with self._lock:
            now = self._clock()
            cooldown = MODEL_RETRY_SECONDS if self._error else MODEL_REFRESH_DEBOUNCE_SECONDS
            if now - self._last_attempt < cooldown:
                return self._snapshot()
            if not force and not self._error and now - self._last_success < MODEL_CACHE_SECONDS:
                return self._snapshot()
            generation = self._generation
            self._last_attempt = now
            try:
                models = await fetch()
            except ModelDiscoveryError as err:
                if generation == self._generation:
                    self._error = err.reason
                return self._snapshot()
            # Reauthentication may have happened while discovery was in flight.
            if generation != self._generation:
                return self._snapshot()
            self._models = tuple(models)
            self._last_success = self._clock()
            self._error = None
            return replace(self._snapshot(), source="discovered")


class AsyncGetClient(Protocol):
    async def get(self, url: str, **kwargs: Any) -> Any: ...


async def fetch_codex_model_ids(
    *, http_client: AsyncGetClient, access_token: str | None
) -> list[str]:
    """Return only advertised visible models, or raise a classified discovery error."""
    if not access_token:
        raise ModelDiscoveryError("authentication")
    try:
        response = await http_client.get(
            CODEX_MODELS_URL, headers=codex_headers(access_token), timeout=10
        )
    except httpx.HTTPError as err:
        raise ModelDiscoveryError() from err
    if response.status_code == 401:
        raise ModelDiscoveryError("authentication")
    if response.status_code != 200:
        raise ModelDiscoveryError()
    try:
        payload = response.json()
    except ValueError as err:
        raise ModelDiscoveryError("invalid_response") from err
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ModelDiscoveryError("invalid_response")

    ranked: list[tuple[float, str]] = []
    valid_entries = 0
    for item in payload["models"]:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        valid_entries += 1
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = priority if isinstance(priority, int | float) and math.isfinite(priority) else 10_000
        ranked.append((rank, slug.strip()))
    if payload["models"] and not valid_entries:
        raise ModelDiscoveryError("invalid_response")
    ranked.sort(key=lambda item: (item[0], item[1]))
    return list(dict.fromkeys(slug for _, slug in ranked))
