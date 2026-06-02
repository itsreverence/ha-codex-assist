from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

CODEX_BACKEND_BASE_URL = "https://chatgpt.com/backend-api/codex"


class AsyncPostClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class CodexMessage:
    role: str
    content: str


class CodexClient:
    def __init__(
        self,
        *,
        http_client: AsyncPostClient,
        access_token: str,
        base_url: str = CODEX_BACKEND_BASE_URL,
    ) -> None:
        self._http_client = http_client
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")

    async def generate_text(
        self,
        *,
        model: str,
        instructions: str,
        messages: list[CodexMessage],
    ) -> str:
        response = await self._http_client.post(
            f"{self._base_url}/responses",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "instructions": instructions,
                "input": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                "store": False,
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"Codex request failed with status {response.status_code}")
        return extract_output_text(response.json())


def extract_output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts).strip()
