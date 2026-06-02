import base64
import json

import pytest

from custom_components.codex_assist.codex_auth import CodexTokenSet
from custom_components.codex_assist.codex_runtime import resolve_runtime_tokens


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
