import base64
import json

import httpx
import pytest

from custom_components.codex_assist.codex_auth import (
    CodexAuthClient,
    CodexAuthTemporaryError,
    CodexReauthRequiredError,
    CodexTokenSet,
)
from custom_components.codex_assist.codex_runtime import (
    RuntimeTokenCoordinator,
    resolve_runtime_tokens,
)


def _jwt_with_exp(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class FakeAuthClient:
    def __init__(self, refreshed):
        self.refreshed = refreshed
        self.calls = []

    async def refresh(self, tokens):
        self.calls.append(tokens)
        return self.refreshed


class FailingAuthClient:
    def __init__(self, error):
        self.error = error

    async def refresh(self, tokens):
        raise self.error


@pytest.mark.asyncio
async def test_resolve_runtime_tokens_keeps_valid_access_token_without_update():
    updates = []
    auth = FakeAuthClient(CodexTokenSet("access-2", "refresh-2"))
    data = {"access_token": _jwt_with_exp(2_000), "refresh_token": "refresh-1"}

    tokens = await resolve_runtime_tokens(
        data,
        auth_client=auth,
        async_update_entry_data=lambda updated: updates.append(updated),
        now=1_000,
    )

    assert tokens.access_token == data["access_token"]
    assert tokens.refresh_token == "refresh-1"
    assert auth.calls == []
    assert updates == []


@pytest.mark.asyncio
async def test_resolve_runtime_tokens_refreshes_expiring_access_token_and_persists_rotation():
    updates = []
    auth = FakeAuthClient(CodexTokenSet("access-2", "refresh-2"))
    data = {
        "model": "gpt-5.4",
        "access_token": _jwt_with_exp(1_060),
        "refresh_token": "refresh-1",
    }

    tokens = await resolve_runtime_tokens(
        data,
        auth_client=auth,
        async_update_entry_data=lambda updated: updates.append(updated),
        now=1_000,
    )

    assert tokens == CodexTokenSet(access_token="access-2", refresh_token="refresh-2")
    assert auth.calls == [CodexTokenSet(data["access_token"], "refresh-1")]
    assert updates == [
        {
            "model": "gpt-5.4",
            "access_token": "access-2",
            "refresh_token": "refresh-2",
        }
    ]


@pytest.mark.asyncio
async def test_resolve_runtime_tokens_requires_refresh_token_to_refresh_expired_token():
    auth = FakeAuthClient(CodexTokenSet("access-2", "refresh-2"))

    with pytest.raises(RuntimeError, match="missing refresh_token"):
        await resolve_runtime_tokens(
            {"access_token": _jwt_with_exp(999)},
            auth_client=auth,
            async_update_entry_data=lambda updated: None,
            now=1_000,
        )


@pytest.mark.asyncio
async def test_resolve_runtime_tokens_keeps_current_token_when_refresh_is_temporarily_blocked():
    data = {"access_token": _jwt_with_exp(1_060), "refresh_token": "refresh-1"}

    tokens = await resolve_runtime_tokens(
        data,
        auth_client=FailingAuthClient(CodexAuthTemporaryError("rate limited")),
        async_update_entry_data=lambda updated: None,
        now=1_000,
    )

    assert tokens == CodexTokenSet(data["access_token"], "refresh-1")


@pytest.mark.asyncio
async def test_resolve_runtime_tokens_raises_temporary_error_when_access_token_is_expired():
    with pytest.raises(CodexAuthTemporaryError):
        await resolve_runtime_tokens(
            {"access_token": _jwt_with_exp(999), "refresh_token": "refresh-1"},
            auth_client=FailingAuthClient(CodexAuthTemporaryError("rate limited")),
            async_update_entry_data=lambda updated: None,
            now=1_000,
        )


@pytest.mark.parametrize("mode", ["near_expiry", "expired", "rejected"])
@pytest.mark.parametrize("failure", ["transport", "invalid_json", "invalid_token", "reauth"])
async def test_coordinator_classifies_real_auth_failures_without_persisting(mode, failure):
    data = {
        "access_token": _jwt_with_exp(999 if mode == "expired" else 1060),
        "refresh_token": "test-refresh",
    }
    original = dict(data)
    updates = []
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "transport":
            raise httpx.ConnectError("synthetic transport failure", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not json")
        if failure == "reauth":
            return httpx.Response(401)
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        coordinator = RuntimeTokenCoordinator()
        kwargs = {"auth_client": CodexAuthClient(http), "async_update_entry_data": updates.append}
        if mode == "rejected":
            resolving = coordinator.refresh_after_rejection(
                lambda: data, rejected_tokens=CodexTokenSet(**data), **kwargs
            )
        else:
            resolving = coordinator.resolve(lambda: data, now=1000, **kwargs)
        if mode == "near_expiry" and failure != "reauth":
            assert await resolving == CodexTokenSet(**original)
        else:
            error_type = (
                CodexReauthRequiredError if failure == "reauth" else CodexAuthTemporaryError
            )
            with pytest.raises(error_type):
                await resolving

    assert len(requests) == 1
    assert updates == []
    assert data == original
