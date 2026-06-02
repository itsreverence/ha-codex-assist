import pytest

from custom_components.codex_assist.codex_auth import CodexAuthClient, CodexTokenSet


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_refresh_uses_openai_token_endpoint_and_returns_new_tokens():
    http = FakeHttpClient([
        FakeResponse(200, {"access_token": "access-2", "refresh_token": "refresh-2"})
    ])
    client = CodexAuthClient(http_client=http)

    tokens = await client.refresh(CodexTokenSet(access_token="access-1", refresh_token="refresh-1"))

    assert tokens.access_token == "access-2"
    assert tokens.refresh_token == "refresh-2"
    assert http.calls == [
        (
            "https://auth.openai.com/oauth/token",
            {
                "data": {
                    "grant_type": "refresh_token",
                    "refresh_token": "refresh-1",
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                },
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_refresh_keeps_existing_refresh_token_when_openai_omits_rotation():
    http = FakeHttpClient([FakeResponse(200, {"access_token": "access-2"})])
    client = CodexAuthClient(http_client=http)

    tokens = await client.refresh(CodexTokenSet(access_token="access-1", refresh_token="refresh-1"))

    assert tokens.access_token == "access-2"
    assert tokens.refresh_token == "refresh-1"


@pytest.mark.asyncio
async def test_refresh_raises_clear_error_on_failed_response():
    http = FakeHttpClient([FakeResponse(401, {"error": "invalid_grant"})])
    client = CodexAuthClient(http_client=http)

    with pytest.raises(RuntimeError, match="Codex token refresh failed with status 401"):
        await client.refresh(CodexTokenSet(access_token="access-1", refresh_token="refresh-1"))
