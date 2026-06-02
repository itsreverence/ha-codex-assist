from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"


class AsyncPostClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class CodexTokenSet:
    access_token: str
    refresh_token: str


class CodexAuthClient:
    def __init__(self, http_client: AsyncPostClient) -> None:
        self._http_client = http_client

    async def refresh(self, tokens: CodexTokenSet) -> CodexTokenSet:
        response = await self._http_client.post(
            CODEX_OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Codex token refresh failed with status {response.status_code}")
        payload = response.json()
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Codex token refresh response was missing access_token")
        refresh_token = str(payload.get("refresh_token") or tokens.refresh_token).strip()
        return CodexTokenSet(access_token=access_token, refresh_token=refresh_token)
