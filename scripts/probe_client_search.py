"""Bounded, data-minimized probe of the integration's real client search path.

Requires integration-owned authentication. Never borrow another application's
credentials. No HA connection, devices, attachments, or private prompts are used.
Output contains counts and booleans, not tokens, answers, URLs or native state.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.codex_assist.codex_client import (  # noqa: E402
    CodexClient,
    CodexResponseItemDelta,
    CodexTextDelta,
)

PROMPT = (
    "Use web_search to find who maintains the IANA Reserved Domains page, "
    "then answer briefly. Do not answer from memory."
)
INSTRUCTIONS = (
    "This is a public-documentation search test. Use only public IANA documentation. "
    "Do not request private information or Home Assistant data."
)


async def probe(model: str, token: str, *, transport=None) -> dict:
    """Exercise direct query/open, model-driven search, and native replay."""
    requests = []

    async def observe(response):
        path = response.request.url.path
        route = "search" if path.endswith("/alpha/search") else "responses"
        requests.append({"route": route, "status": response.status_code})

    search_results = []

    class ObservedClient(CodexClient):
        async def web_search(self, model, arguments, settings):
            before = len(requests)
            result = await super().web_search(model, arguments, settings)
            attempts = requests[before:]
            # Observe the real normalized result, including internally dispatched
            # calls. A reply after tool failure is not a successful search gate.
            search_results.append(
                len(attempts) == 1 and attempts[0] == {"route": "search", "status": 200}
                and bool(result.text.strip())
                and not result.text.startswith((
                    "Web search failed", "Web search returned", "Web search found nothing",
                ))
            )
            return result

    async with httpx.AsyncClient(
        timeout=60, transport=transport, event_hooks={"response": [observe]},
    ) as http:
        client = ObservedClient(http_client=http, access_token=token)
        for arguments in (
            {"queries": ["site:iana.org reserved domains"]},
            {"queries": [], "open_urls": ["https://www.iana.org/help/example-domains"]},
        ):
            await client.web_search(model, arguments, {})
            if not search_results[-1]:
                return {"result": "direct_search_failed", "requests": requests}

        original = [{"role": "user", "content": PROMPT}]
        native = []
        visible = []
        before = len(search_results)
        async for delta in client.stream_turn(
            model=model, instructions=INSTRUCTIONS, input_items=original,
            tools=[{"type": "web_search"}],
        ):
            if isinstance(delta, CodexResponseItemDelta):
                native.append(delta.item)
            elif isinstance(delta, CodexTextDelta):
                visible.append(delta.text)
        searched = bool(search_results[before:])
        answered = bool("".join(visible).strip())
        if not searched or not all(search_results[before:]) or not answered or not native:
            return {
                "result": "model_search_not_established", "requests": requests,
                "searched": searched, "answered": answered, "native_items": len(native),
            }

        followup = []
        async for delta in client.stream_turn(
            model=model, instructions=INSTRUCTIONS,
            input_items=[*original, *native, {
                "role": "user", "content": "Restate the organization name from that result.",
            }], tools=[],
        ):
            if isinstance(delta, CodexTextDelta):
                followup.append(delta.text)
        return {
            "result": "completed" if "".join(followup).strip() else "empty_followup",
            "requests": requests, "searched": searched, "answered": answered,
            "native_items": len(native), "followup_answered": bool("".join(followup).strip()),
            "semantic_correctness": "not_assessed",
        }


async def run(model: str, token: str) -> int:
    try:
        result = await asyncio.wait_for(probe(model, token), timeout=180)
    except Exception as exc:
        # Backend exception text can contain response payloads: never print it.
        print(json.dumps({"result": "failed", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="An account-discovered model ID")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps({
            "model": args.model,
            "stages": ["direct_query", "direct_url_open", "model_search", "native_replay"],
            "live_request_sent": False,
        }, sort_keys=True))
        return 0
    token = os.environ.get("CODEX_ASSIST_ACCESS_TOKEN", "").strip()
    if not token:
        print("Integration-owned token unavailable; live probe not run.", file=sys.stderr)
        return 2
    return asyncio.run(run(args.model, token))


if __name__ == "__main__":
    raise SystemExit(main())
