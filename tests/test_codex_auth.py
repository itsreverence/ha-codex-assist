import asyncio
import traceback

import httpx
import pytest

from custom_components.codex_assist.codex_auth import (
    CodexAuthClient,
    CodexAuthorizationCode,
    CodexAuthTemporaryError,
    CodexReauthRequiredError,
    CodexTokenSet,
)


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

    with pytest.raises(CodexReauthRequiredError, match="status 401"):
        await client.refresh(CodexTokenSet(access_token="access-1", refresh_token="refresh-1"))


@pytest.mark.asyncio
async def test_refresh_treats_rate_limit_as_temporary_auth_failure():
    http = FakeHttpClient([FakeResponse(429, {"error": "rate_limited"})])
    client = CodexAuthClient(http_client=http)

    with pytest.raises(CodexAuthTemporaryError, match="status 429"):
        await client.refresh(CodexTokenSet(access_token="access-1", refresh_token="refresh-1"))


AUTH_OPERATIONS = [
    ("request_device_code", {}),
    ("poll_device_code", {"device_auth_id": "test-device", "user_code": "TEST-CODE"}),
    ("exchange_authorization_code", {
        "authorization": CodexAuthorizationCode("test-code", "test-verifier"),
    }),
    ("refresh", {"tokens": CodexTokenSet("test-access", "test-refresh")}),
]


@pytest.mark.parametrize(("operation", "kwargs"), AUTH_OPERATIONS)
@pytest.mark.parametrize("error_type", [
    httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.HTTPStatusError,
])
async def test_auth_transport_failure_is_temporary_and_sanitized(operation, kwargs, error_type):
    requests = []

    def respond(request):
        requests.append(request)
        if error_type is httpx.HTTPStatusError:
            raise error_type(
                "private-auth-marker", request=request, response=httpx.Response(502)
            )
        raise error_type("private-auth-marker", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(CodexAuthTemporaryError) as caught:
            await getattr(CodexAuthClient(http), operation)(**kwargs)

    assert len(requests) == 1
    assert error_type.__name__ in str(caught.value)
    assert "private-auth-marker" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(("operation", "kwargs"), AUTH_OPERATIONS)
@pytest.mark.parametrize("content", [b"private-auth-marker", b"[]", b"null", b'"text"', b"1"])
async def test_auth_invalid_json_response_is_temporary(operation, kwargs, content):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    ) as http:
        with pytest.raises(CodexAuthTemporaryError) as caught:
            await getattr(CodexAuthClient(http), operation)(**kwargs)

    assert "private-auth-marker" not in "".join(traceback.format_exception(caught.value))
    if content == b"private-auth-marker":
        assert caught.value.__suppress_context__


@pytest.mark.parametrize(("operation", "kwargs", "payload", "field"), [
    (*AUTH_OPERATIONS[0], {"user_code": "test-code", "device_auth_id": "test-device"}, field)
    for field in ("user_code", "device_auth_id")
] + [
    (
        *AUTH_OPERATIONS[1],
        {"authorization_code": "test-code", "code_verifier": "test-verifier"},
        field,
    )
    for field in ("authorization_code", "code_verifier")
] + [
    (
        *AUTH_OPERATIONS[index],
        {"access_token": "test-access", "refresh_token": "test-refresh"},
        field,
    )
    for index in (2, 3) for field in ("access_token", "refresh_token")
])
@pytest.mark.parametrize("invalid", [None, "", "  ", 123, True, ["private-auth-marker"],
                                     {"secret": "private-auth-marker"}])
async def test_auth_validates_credentials_and_preserves_optional_refresh(
    operation, kwargs, payload, field, invalid
):
    payload = {**payload, field: invalid}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        if operation == "refresh" and field == "refresh_token" and invalid in (None, "", "  "):
            assert await CodexAuthClient(http).refresh(**kwargs) == CodexTokenSet(
                "test-access", "test-refresh"
            )
        else:
            with pytest.raises(CodexAuthTemporaryError) as caught:
                await getattr(CodexAuthClient(http), operation)(**kwargs)
            assert "private-auth-marker" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(("operation", "kwargs"), AUTH_OPERATIONS)
async def test_auth_missing_credentials_are_temporary(operation, kwargs):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as http:
        with pytest.raises(CodexAuthTemporaryError):
            await getattr(CodexAuthClient(http), operation)(**kwargs)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_refresh_status_classification_precedes_response_decoding(status):
    error_type = CodexReauthRequiredError if status in {400, 401, 403} else CodexAuthTemporaryError
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=b"not json"))
    ) as http:
        with pytest.raises(error_type):
            await CodexAuthClient(http).refresh(CodexTokenSet("test-access", "test-refresh"))


@pytest.mark.parametrize(("operation", "kwargs"), AUTH_OPERATIONS)
async def test_auth_cancellation_propagates(operation, kwargs):
    def respond(request):
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(asyncio.CancelledError):
            await getattr(CodexAuthClient(http), operation)(**kwargs)
