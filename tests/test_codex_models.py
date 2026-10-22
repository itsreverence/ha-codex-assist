import asyncio

import httpx
import pytest

from custom_components.codex_assist.codex_models import (
    CODEX_MODELS_URL,
    DEFAULT_CODEX_MODELS,
    MODEL_CACHE_SECONDS,
    MODEL_RETRY_SECONDS,
    ModelDiscoveryCache,
    ModelDiscoveryError,
    fetch_codex_model_ids,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


async def test_discovery_returns_only_visible_advertised_models_in_priority_order():
    http = FakeHttpClient(
        [
            FakeResponse(
                200,
                {
                    "models": [
                        {"slug": "gpt-5.5", "priority": 30},
                        {"slug": "hidden-model", "priority": 1, "visibility": "hide"},
                        {"slug": "future-model", "priority": 10},
                        {"slug": "future-model", "priority": 12},
                        {"slug": " gpt-5.5 ", "priority": 30},
                    ]
                },
            )
        ]
    )
    models = await fetch_codex_model_ids(http_client=http, access_token="token-1")
    assert models == ["future-model", "gpt-5.5"]
    assert http.calls[0][0] == CODEX_MODELS_URL
    assert http.calls[0][1]["headers"]["Authorization"] == "Bearer token-1"
    assert http.calls[0][1]["timeout"] == 10


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (FakeResponse(401, {}), "authentication"),
        (FakeResponse(403, {}), "unavailable"),
        (FakeResponse(429, {}), "unavailable"),
        (FakeResponse(500, {}), "unavailable"),
        (httpx.ReadTimeout("private request details"), "unavailable"),
        (FakeResponse(200, ValueError("private response")), "invalid_response"),
        (FakeResponse(200, {}), "invalid_response"),
        (FakeResponse(200, {"models": {}}), "invalid_response"),
        (FakeResponse(200, {"models": [None, {"slug": " "}]}), "invalid_response"),
    ],
)
async def test_discovery_classifies_failure_without_returning_guessed_models(response, reason):
    with pytest.raises(ModelDiscoveryError) as err:
        await fetch_codex_model_ids(http_client=FakeHttpClient([response]), access_token="test")
    assert str(err.value) == reason


async def test_missing_credentials_do_not_make_a_request():
    http = FakeHttpClient([])
    with pytest.raises(ModelDiscoveryError, match="authentication"):
        await fetch_codex_model_ids(http_client=http, access_token=None)
    assert not http.calls


@pytest.mark.parametrize("entries", [[], [{"slug": "hidden", "visibility": "hidden"}]])
async def test_successful_empty_discovery_does_not_become_a_fallback(entries):
    assert (
        await fetch_codex_model_ids(
            http_client=FakeHttpClient([FakeResponse(200, {"models": entries})]),
            access_token="test",
        )
        == []
    )


async def test_nonfinite_priorities_and_malformed_entries_do_not_break_valid_results():
    entries = [
        None,
        {},
        {"slug": "a", "priority": float("nan")},
        {"slug": "b", "priority": float("inf")},
        {"slug": "c", "priority": 1},
    ]
    assert await fetch_codex_model_ids(
        http_client=FakeHttpClient([FakeResponse(200, {"models": entries})]), access_token="test"
    ) == ["c", "a", "b"]


async def test_cache_reuses_success_without_fetching_until_it_expires():
    now = [0]
    cache = ModelDiscoveryCache(clock=lambda: now[0])
    calls = []

    async def fetch():
        calls.append(True)
        return ["model-1"]

    await cache.async_get(fetch)
    now[0] = MODEL_CACHE_SECONDS - 1
    reused = await cache.async_get(fetch)

    assert (reused.source, reused.models, reused.error) == ("cached", ("model-1",), None)
    assert len(calls) == 1


async def test_cache_keeps_last_success_on_failure_and_retries_after_backoff():
    now = [0]
    cache = ModelDiscoveryCache(clock=lambda: now[0])
    calls = []

    async def fetch():
        calls.append(True)
        if len(calls) == 2:
            raise ModelDiscoveryError()
        return [f"model-{len(calls)}"]

    assert (await cache.async_get(fetch)).source == "discovered"
    now[0] = MODEL_CACHE_SECONDS
    stale = await cache.async_get(fetch)
    assert (stale.source, stale.models, stale.error) == ("cached", ("model-1",), "unavailable")
    assert await cache.async_get(fetch, force=True) == stale
    assert len(calls) == 2
    now[0] += MODEL_RETRY_SECONDS
    assert (await cache.async_get(fetch)).models == ("model-3",)


async def test_fallback_is_labeled_only_until_first_success_including_empty_success():
    cache = ModelDiscoveryCache(clock=lambda: now[0])
    now = [0]

    async def fail():
        raise ModelDiscoveryError("authentication")

    fallback = await cache.async_get(fail)
    assert fallback.source == "fallback"
    assert fallback.models == tuple(DEFAULT_CODEX_MODELS)
    assert fallback.error == "authentication"
    now[0] += MODEL_RETRY_SECONDS

    async def empty():
        return []

    assert (await cache.async_get(empty)).models == ()
    now[0] += MODEL_CACHE_SECONDS
    result = await cache.async_get(fail)
    assert result.models == ()
    assert result.source == "cached"


async def test_concurrent_settings_refreshes_share_one_request_and_cache_is_entry_scoped():
    cache = ModelDiscoveryCache()
    calls = []

    async def fetch():
        calls.append(True)
        await asyncio.sleep(0)
        return ["account-model"]

    results = await asyncio.gather(*(cache.async_get(fetch, force=True) for _ in range(3)))
    assert len(calls) == 1
    assert all(r.models == ("account-model",) for r in results)
    await ModelDiscoveryCache().async_get(fetch)
    assert len(calls) == 2


async def test_reauthentication_discards_cache_and_inflight_results():
    cache = ModelDiscoveryCache()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def old_account():
        started.set()
        await finish.wait()
        return ["old-account-model"]

    request = asyncio.create_task(cache.async_get(old_account))
    await started.wait()
    cache.clear()
    finish.set()
    assert (await request).source == "fallback"

    async def new_account():
        return ["new-account-model"]

    assert (await cache.async_get(new_account)).models == ("new-account-model",)
