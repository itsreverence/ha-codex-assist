from __future__ import annotations

from typing import Any, Protocol

from .codex_client import codex_headers

CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"

DEFAULT_CODEX_MODELS = [
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
]

_FORWARD_COMPAT_MODELS = {
    "gpt-5.5": ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"),
    "gpt-5.4-mini": ("gpt-5.3-codex",),
    "gpt-5.4": ("gpt-5.3-codex",),
    "gpt-5.3-codex-spark": ("gpt-5.3-codex",),
}


class AsyncGetClient(Protocol):
    async def get(self, url: str, **kwargs: Any) -> Any: ...


async def fetch_codex_model_ids(
    *,
    http_client: AsyncGetClient,
    access_token: str | None,
) -> list[str]:
    """Fetch the Codex backend's visible model slugs for this ChatGPT/Codex account."""

    if not access_token:
        return list(DEFAULT_CODEX_MODELS)

    try:
        response = await http_client.get(
            CODEX_MODELS_URL,
            headers=codex_headers(access_token),
            timeout=10,
        )
    except Exception:
        return list(DEFAULT_CODEX_MODELS)

    if response.status_code != 200:
        return list(DEFAULT_CODEX_MODELS)

    try:
        payload = response.json()
    except Exception:
        return list(DEFAULT_CODEX_MODELS)

    entries = payload.get("models", []) if isinstance(payload, dict) else []
    ranked: list[tuple[int, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, int | float) else 10_000
        ranked.append((rank, slug.strip()))

    if not ranked:
        return list(DEFAULT_CODEX_MODELS)

    ranked.sort(key=lambda item: (item[0], item[1]))
    return add_forward_compat_models([slug for _, slug in ranked])


def add_forward_compat_models(model_ids: list[str]) -> list[str]:
    """Keep newer known Codex slugs visible when backend discovery lags behind."""

    ordered: list[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if model_id not in seen:
            ordered.append(model_id)
            seen.add(model_id)

    for synthetic_model, template_models in _FORWARD_COMPAT_MODELS.items():
        if synthetic_model in seen:
            continue
        if any(template in seen for template in template_models):
            ordered.append(synthetic_model)
            seen.add(synthetic_model)

    return ordered
