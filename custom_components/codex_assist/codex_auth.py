from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
CODEX_DEVICE_AUTH_USER_CODE_URL = (
    "https://auth.openai.com/api/accounts/deviceauth/usercode"
)
CODEX_DEVICE_AUTH_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CODEX_DEVICE_VERIFICATION_URL = "https://auth.openai.com/codex/device"


class AsyncPostClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class CodexDeviceCode:
    user_code: str
    device_auth_id: str
    verification_uri: str
    interval: int


@dataclass(frozen=True)
class CodexAuthorizationCode:
    authorization_code: str
    code_verifier: str


@dataclass(frozen=True)
class CodexTokenSet:
    access_token: str
    refresh_token: str


class CodexAuthClient:
    def __init__(self, http_client: AsyncPostClient) -> None:
        self._http_client = http_client

    async def request_device_code(self) -> CodexDeviceCode:
        response = await self._http_client.post(
            CODEX_DEVICE_AUTH_USER_CODE_URL,
            json={"client_id": CODEX_OAUTH_CLIENT_ID},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Codex device-code request failed with status {response.status_code}"
            )
        payload = response.json()
        user_code = str(payload.get("user_code") or "").strip()
        device_auth_id = str(payload.get("device_auth_id") or "").strip()
        if not user_code or not device_auth_id:
            raise RuntimeError("Codex device-code response missing required fields")
        interval = _positive_int(payload.get("interval"), default=5)
        return CodexDeviceCode(
            user_code=user_code,
            device_auth_id=device_auth_id,
            verification_uri=CODEX_DEVICE_VERIFICATION_URL,
            interval=interval,
        )

    async def poll_device_code(
        self,
        *,
        device_auth_id: str,
        user_code: str,
    ) -> CodexAuthorizationCode | None:
        response = await self._http_client.post(
            CODEX_DEVICE_AUTH_TOKEN_URL,
            json={"device_auth_id": device_auth_id, "user_code": user_code},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code in {403, 404}:
            return None
        if response.status_code != 200:
            raise RuntimeError(
                f"Codex device-code polling failed with status {response.status_code}"
            )
        payload = response.json()
        authorization_code = str(payload.get("authorization_code") or "").strip()
        code_verifier = str(payload.get("code_verifier") or "").strip()
        if not authorization_code or not code_verifier:
            raise RuntimeError("Codex device auth response missing exchange fields")
        return CodexAuthorizationCode(
            authorization_code=authorization_code,
            code_verifier=code_verifier,
        )

    async def exchange_authorization_code(
        self,
        authorization: CodexAuthorizationCode,
    ) -> CodexTokenSet:
        response = await self._http_client.post(
            CODEX_OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": authorization.authorization_code,
                "redirect_uri": CODEX_OAUTH_REDIRECT_URI,
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "code_verifier": authorization.code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Codex token exchange failed with status {response.status_code}"
            )
        return _token_set_from_payload(response.json())

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
            raise RuntimeError(
                f"Codex token refresh failed with status {response.status_code}"
            )
        return _token_set_from_payload(response.json(), fallback_refresh=tokens.refresh_token)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _token_set_from_payload(
    payload: dict[str, Any],
    *,
    fallback_refresh: str = "",
) -> CodexTokenSet:
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Codex token response was missing access_token")
    refresh_token = str(payload.get("refresh_token") or fallback_refresh).strip()
    return CodexTokenSet(access_token=access_token, refresh_token=refresh_token)
