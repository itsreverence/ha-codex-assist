import pytest

from custom_components.codex_assist.codex_models import (
    CODEX_MODELS_URL,
    DEFAULT_CODEX_MODELS,
    add_forward_compat_models,
    fetch_codex_model_ids,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("no fake response queued")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_fetch_codex_model_ids_uses_codex_models_endpoint_and_priority_order():
    http = FakeHttpClient(
        [
            FakeResponse(
                200,
                {
                    "models": [
                        {"slug": "gpt-5.3-codex", "priority": 30},
                        {"slug": "hidden-model", "priority": 1, "visibility": "hide"},
                        {"slug": "gpt-5.5", "priority": 10},
                    ]
                },
            )
        ]
    )

    models = await fetch_codex_model_ids(http_client=http, access_token="token-1")

    assert models == ["gpt-5.5", "gpt-5.3-codex", "gpt-5.4-mini", "gpt-5.4", "gpt-5.3-codex-spark"]
    assert http.calls[0][0] == CODEX_MODELS_URL
    assert http.calls[0][1]["headers"]["Authorization"] == "Bearer token-1"


@pytest.mark.asyncio
async def test_fetch_codex_model_ids_falls_back_when_discovery_fails():
    http = FakeHttpClient([FakeResponse(401, {"error": "invalid_token"})])

    models = await fetch_codex_model_ids(http_client=http, access_token="bad-token")

    assert models == DEFAULT_CODEX_MODELS


@pytest.mark.asyncio
async def test_fetch_codex_model_ids_falls_back_without_token():
    http = FakeHttpClient([])

    models = await fetch_codex_model_ids(http_client=http, access_token=None)

    assert models == DEFAULT_CODEX_MODELS
    assert http.calls == []


def test_add_forward_compat_models_preserves_order_and_adds_known_codex_slugs():
    assert add_forward_compat_models(["gpt-5.3-codex"]) == [
        "gpt-5.3-codex",
        "gpt-5.5",
        "gpt-5.4-mini",
        "gpt-5.4",
        "gpt-5.3-codex-spark",
    ]
