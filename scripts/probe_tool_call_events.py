"""Probe how the Codex backend streams function calls, and how the client parses them.

The client takes each tool call from its completed ``response.output_item.done``
item. This probe sends one fixed request that asks for two parallel calls to a
harmless tool, records the raw stream while the real client consumes it, and
reports whether the completed items carry the full arguments the client used.

Requires integration-owned authentication: ``CODEX_ASSIST_ACCESS_TOKEN``, or
``--sign-in``, which runs the integration's own device-code flow and keeps the
token in memory only. Never borrow another application's credentials. Output
contains counts and booleans, not arguments, identifiers, tokens or text.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.codex_assist.codex_auth import CodexAuthClient  # noqa: E402
from custom_components.codex_assist.codex_client import (  # noqa: E402
    CodexClient,
    CodexToolCallDelta,
)
from custom_components.codex_assist.codex_models import fetch_codex_model_ids  # noqa: E402

PROMPT = (
    "Call record_word twice in parallel: once with the word alpha and once with "
    "the word beta. After both results, reply with the single word done."
)
INSTRUCTIONS = "This is a tool-calling format test. Use only the record_word tool."
TOOL = {
    "type": "function",
    "name": "record_word",
    "description": "Record one word for a format test.",
    "parameters": {
        "type": "object",
        "properties": {"word": {"type": "string"}},
        "required": ["word"],
        "additionalProperties": False,
    },
    "strict": False,
}


class _Tee(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, sink: bytearray) -> None:
        self._inner = inner
        self._sink = sink

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._sink.extend(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class RecordingTransport(httpx.AsyncBaseTransport):
    """Pass requests through and keep a copy of the last response body."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.status_code = 0
        self.body = bytearray()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        self.status_code = response.status_code
        self.body = bytearray()
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_Tee(response.stream, self.body),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def _object_arguments(value: Any) -> dict | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _raw_events(body: bytes) -> list[dict]:
    """Parse SSE data lines without the client's parser, so its bugs can't hide."""
    events = []
    for line in body.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        try:
            event = json.loads(line[5:])
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def summarize(
    body: bytes, client_calls: list[tuple[str, dict]], client_error: str | None
) -> dict[str, Any]:
    """Compare the raw function-call events with the calls the client yielded."""
    items: list[dict] = []
    announced: set[str] = set()
    arguments_done: dict[str, Any] = {}
    completed = False
    for event in _raw_events(body):
        kind = event.get("type")
        item = event.get("item")
        is_call_item = isinstance(item, dict) and item.get("type") == "function_call"
        if kind == "response.output_item.added" and is_call_item:
            announced.add(str(item.get("id")))
        elif kind in {"response.function_call_arguments.delta",
                      "response.function_call_arguments.done"}:
            announced.add(str(event.get("item_id")))
            if kind == "response.function_call_arguments.done":
                arguments_done[str(event.get("item_id"))] = event.get("arguments")
        elif kind == "response.output_item.done" and is_call_item:
            items.append(item)
        elif kind == "response.completed":
            completed = True

    item_arguments = {str(item.get("id")): item.get("arguments") for item in items}
    call_ids = [str(item.get("call_id") or "") for item in items]
    expected = [(call_id, _object_arguments(item.get("arguments")))
                for call_id, item in zip(call_ids, items, strict=True)]
    checks = {
        "completed": completed,
        "items_have_object_arguments": bool(items) and all(
            _object_arguments(item.get("arguments")) is not None for item in items
        ),
        "announced_calls_completed": announced <= set(item_arguments),
        "call_ids_unique": all(call_ids) and len(set(call_ids)) == len(call_ids),
        "arguments_done_match_items": all(
            arguments_done[key] == value
            for key, value in item_arguments.items() if key in arguments_done
        ),
        "client_matches_items": client_error is None and client_calls == expected,
    }
    return {
        "function_call_items": len(items),
        "arguments_done_events": len(arguments_done),
        "client_tool_calls": len(client_calls),
        "client_error": client_error,
        **checks,
        "result": (
            "inconclusive" if not items and client_error is None
            else "consistent" if all(checks.values()) else "inconsistent"
        ),
    }


async def run_probe(
    *, model: str, access_token: str, transport: httpx.AsyncBaseTransport
) -> dict[str, Any]:
    recorder = RecordingTransport(transport)
    calls: list[tuple[str, dict]] = []
    client_error = None
    async with httpx.AsyncClient(transport=recorder, timeout=60) as http:
        client = CodexClient(http_client=http, access_token=access_token)
        try:
            async with asyncio.timeout(120):
                async for delta in client.stream_turn(
                    model=model,
                    instructions=INSTRUCTIONS,
                    input_items=[{"role": "user", "content": PROMPT}],
                    tools=[TOOL],
                ):
                    if isinstance(delta, CodexToolCallDelta):
                        calls.append((delta.tool_call.id, delta.tool_call.arguments))
        except Exception as err:  # noqa: BLE001 - reported by type only
            client_error = type(err).__name__
    return {"http_status": recorder.status_code,
            **summarize(bytes(recorder.body), calls, client_error)}


def synthetic_transport() -> httpx.MockTransport:
    """A stream shaped like the live backend's, for --dry-run."""
    events: list[dict[str, Any]] = []
    for index, word in enumerate(("alpha", "beta")):
        item = {"id": f"fc_{index}", "type": "function_call", "call_id": f"call_{index}",
                "name": "record_word"}
        arguments = json.dumps({"word": word})
        events += [
            {"type": "response.output_item.added", "output_index": index,
             "item": {**item, "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": item["id"],
             "output_index": index, "delta": arguments},
            {"type": "response.function_call_arguments.done", "item_id": item["id"],
             "output_index": index, "arguments": arguments},
            {"type": "response.output_item.done", "output_index": index,
             "item": {**item, "arguments": arguments, "status": "completed"}},
        ]
    events.append({"type": "response.completed", "response": {"status": "completed"}})
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
    return httpx.MockTransport(lambda request: httpx.Response(200, content=body))


async def sign_in() -> str:
    async with httpx.AsyncClient(timeout=30) as http:
        auth = CodexAuthClient(http)
        device = await auth.request_device_code()
        print(f"Open {device.verification_uri} and enter {device.user_code}", file=sys.stderr,
              flush=True)
        async with asyncio.timeout(900):
            while (
                authorization := await auth.poll_device_code(
                    device_auth_id=device.device_auth_id, user_code=device.user_code
                )
            ) is None:
                await asyncio.sleep(device.interval)
        return (await auth.exchange_authorization_code(authorization)).access_token


async def resolve_model(model: str, access_token: str) -> str:
    if model != "discovered":
        return model
    async with httpx.AsyncClient(timeout=30) as http:
        return (await fetch_codex_model_ids(http_client=http, access_token=access_token))[0]


async def main_async(args: argparse.Namespace) -> int:
    if args.dry_run:
        summary = await run_probe(model=args.model, access_token="dry-run",
                                  transport=synthetic_transport())
    else:
        token = os.environ.get("CODEX_ASSIST_ACCESS_TOKEN", "").strip()
        if not token and not args.sign_in:
            print("No integration-owned CODEX_ASSIST_ACCESS_TOKEN is available and --sign-in "
                  "was not given; live probe not run.", file=sys.stderr)
            return 2
        try:
            token = token or await sign_in()
            model = await resolve_model(args.model, token)
            summary = {"model": model, **await run_probe(
                model=model, access_token=token, transport=httpx.AsyncHTTPTransport()
            )}
        except Exception as err:  # noqa: BLE001 - error text may carry account details
            summary = {"result": "error", "error_type": type(err).__name__}
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["result"] == "consistent" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model", required=True,
        help="Model ID from your account's discovered list, or 'discovered' for the first one",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sign-in", action="store_true",
                        help="Run the integration's device-code sign-in; the token stays in memory")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
