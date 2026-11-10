from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.codex_assist.codex_client import (  # noqa: E402
    CODEX_BACKEND_BASE_URL,
    _aiter_completed_response_events,
    codex_headers,
)

FIXED_PROMPT = (
    "Use web search to identify the organization that maintains the IANA "
    "Reserved Domains page. Cite the source in the response."
)


def probe_payload(model: str) -> dict[str, Any]:
    """Return the fixed, data-minimized probe payload."""
    return {
        "model": model,
        "instructions": (
            "Answer only from the hosted web-search tool. Do not use or request "
            "Home Assistant state, user data, attachments, or prior conversation context."
        ),
        "input": [{"role": "user", "content": FIXED_PROMPT}],
        "tools": [
            {
                "type": "web_search",
                "external_web_access": True,
                "filters": {"allowed_domains": ["iana.org"]},
                "search_context_size": "low",
            }
        ],
        "include": ["web_search_call.action.sources"],
        "stream": True,
        "store": False,
    }


async def run_probe(*, model: str, access_token: str) -> int:
    """Run the one-shot hosted-tool probe and emit sanitized JSON lines."""
    url = f"{CODEX_BACKEND_BASE_URL}/responses"
    searched = False
    search_failed = False
    answered = False
    try:
        async with asyncio.timeout(90):
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream(
                    "POST", url, headers=codex_headers(access_token), json=probe_payload(model),
                ) as response:
                    if response.status_code != 200:
                        print(json.dumps({
                            "http_status": response.status_code, "result": "request_rejected",
                        }, sort_keys=True))
                        return 1
                    async for event in _aiter_completed_response_events(response):
                        item = event.get("item")
                        items = (
                            [item] if event.get("type") == "response.output_item.done"
                            and isinstance(item, dict) else []
                        )
                        completed = event.get("response")
                        if (
                            event.get("type") == "response.completed"
                            and isinstance(completed, dict)
                        ):
                            items.extend(completed.get("output") or [])
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            if item.get("type") == "web_search_call":
                                searched |= item.get("status") == "completed"
                                search_failed |= item.get("status") != "completed"
                            for content in item.get("content") or []:
                                if (
                                    isinstance(content, dict)
                                    and content.get("type") == "output_text"
                                ):
                                    text = content.get("text")
                                    answered |= isinstance(text, str) and bool(text.strip())
                        if event.get("type") == "response.output_text.delta":
                            delta = event.get("delta")
                            answered |= isinstance(delta, str) and bool(delta.strip())
    except (httpx.HTTPError, RuntimeError, TimeoutError, ValueError, TypeError):
        print(json.dumps({"result": "failed"}))
        return 1
    result = (
        "completed" if searched and answered and not search_failed else "search_not_established"
    )
    print(json.dumps({"result": result, "searched": searched, "answered": answered}))
    return 0 if result == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model", required=True, help="Model ID selected from your account’s discovered list"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = probe_payload(args.model)
    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    token = os.environ.get("CODEX_ASSIST_ACCESS_TOKEN", "").strip()
    if not token:
        print(
            "No integration-owned CODEX_ASSIST_ACCESS_TOKEN is available; live probe not run.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run_probe(model=args.model, access_token=token))


if __name__ == "__main__":
    raise SystemExit(main())
