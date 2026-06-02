import pytest

from custom_components.codex_assist.codex_auth import (
    CODEX_DEVICE_AUTH_TOKEN_URL,
    CODEX_DEVICE_AUTH_USER_CODE_URL,
    CODEX_DEVICE_VERIFICATION_URL,
    CODEX_OAUTH_CLIENT_ID,
    CODEX_OAUTH_TOKEN_URL,
    CodexAuthClient,
    CodexAuthorizationCode,
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
async def test_request_device_code_posts_codex_json_request_and_returns_pairing_data():
    http = FakeHttpClient(
        [
            FakeResponse(
                200,
                {
                    "user_code": "ABCD-EFGH",
                    "device_auth_id": "device-auth-1",
                    "interval": 7,
                },
            )
        ]
    )
    client = CodexAuthClient(http_client=http)

    code = await client.request_device_code()

    assert code.user_code == "ABCD-EFGH"
    assert code.device_auth_id == "device-auth-1"
    assert code.verification_uri == CODEX_DEVICE_VERIFICATION_URL
    assert code.interval == 7
    assert http.calls == [
        (
            CODEX_DEVICE_AUTH_USER_CODE_URL,
            {
                "json": {"client_id": CODEX_OAUTH_CLIENT_ID},
                "headers": {"Content-Type": "application/json"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_request_device_code_raises_when_required_fields_are_missing():
    http = FakeHttpClient([FakeResponse(200, {"user_code": "ABCD-EFGH"})])
    client = CodexAuthClient(http_client=http)

    with pytest.raises(RuntimeError, match="missing required fields"):
        await client.request_device_code()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 404])
async def test_poll_device_code_returns_none_while_authorization_is_pending(status_code):
    http = FakeHttpClient([FakeResponse(status_code, {"error": "pending"})])
    client = CodexAuthClient(http_client=http)

    result = await client.poll_device_code(
        device_auth_id="device-auth-1",
        user_code="ABCD-EFGH",
    )

    assert result is None
    assert http.calls == [
        (
            CODEX_DEVICE_AUTH_TOKEN_URL,
            {
                "json": {
                    "device_auth_id": "device-auth-1",
                    "user_code": "ABCD-EFGH",
                },
                "headers": {"Content-Type": "application/json"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_poll_device_code_returns_authorization_code_after_user_approves():
    http = FakeHttpClient(
        [
            FakeResponse(
                200,
                {
                    "authorization_code": "authorization-1",
                    "code_verifier": "verifier-1",
                },
            )
        ]
    )
    client = CodexAuthClient(http_client=http)

    result = await client.poll_device_code(
        device_auth_id="device-auth-1",
        user_code="ABCD-EFGH",
    )

    assert result == CodexAuthorizationCode(
        authorization_code="authorization-1",
        code_verifier="verifier-1",
    )


@pytest.mark.asyncio
async def test_exchange_authorization_code_posts_form_and_returns_tokens():
    http = FakeHttpClient(
        [FakeResponse(200, {"access_token": "access-1", "refresh_token": "refresh-1"})]
    )
    client = CodexAuthClient(http_client=http)

    tokens = await client.exchange_authorization_code(
        CodexAuthorizationCode(
            authorization_code="authorization-1",
            code_verifier="verifier-1",
        )
    )

    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert http.calls == [
        (
            CODEX_OAUTH_TOKEN_URL,
            {
                "data": {
                    "grant_type": "authorization_code",
                    "code": "authorization-1",
                    "redirect_uri": "https://auth.openai.com/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": "verifier-1",
                },
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            },
        )
    ]
