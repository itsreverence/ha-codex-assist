"""Bounded, execution-only comparison of client and built-in public web search.

Use only Codex Assist's own credentials. Reports contain sanitized evidence,
never backend content. Dry-run makes no requests and needs no credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from contextlib import aclosing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.codex_assist.codex_client import (
    CodexAuthenticationError,
    CodexClient,
    CodexRateLimitError,
    CodexResponseItemDelta,
    CodexTextDelta,
    CodexToolCallDelta,
)

Arm = Literal["client", "builtin"]
TURN_SECONDS = 60
ARM_SECONDS = 120
TOTAL_SECONDS = 720
DIRECT_URL = "https://www.iana.org/help/example-domains"
SEARCH_TOOL = {
    "type": "web_search", "search_context_size": "medium", "external_web_access": True,
}
INSTRUCTIONS = (
    "This is a public-documentation search test. Use the available web search tool "
    "for the initial question. Do not answer it from memory. Use only public information. "
    "Do not request private information or Home Assistant data. Answer briefly. "
    "For follow-up questions, use the retained conversation evidence without tools."
)
FOLLOWUP = "Restate the key fact from that result using the retained evidence."


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    requires_open: bool = False


CASES = (
    Case(
        "iana_maintainer",
        "Use web search to identify who maintains the IANA Reserved Domains page.",
    ),
    Case("python_stable", "Use web search to identify the current latest stable Python release."),
    Case("iana_direct", f"Open {DIRECT_URL} with web search and state its key fact.", True),
)


@dataclass
class SanitizedTurnEvidence:
    started: bool = False
    response_complete: bool = False
    answered: bool = False
    native_items: int = 0
    unexpected_tool_calls: int = 0
    search_calls: int = 0
    search_completed: int = 0
    open_target_requested: bool = False
    open_target_completed: bool | None = False
    http_requests: int = 0
    response_requests: int = 0
    search_requests: int = 0
    http_error_responses: int = 0
    search_usable_responses: int = 0
    first_visible_text_seconds: float | None = None
    total_seconds: float = 0


@dataclass
class TrialResult:
    case: str
    arm: Arm
    status: str = "not_run"
    error: str = "none"
    gate_pass: bool = False
    initial: SanitizedTurnEvidence = field(default_factory=SanitizedTurnEvidence)
    followup: SanitizedTurnEvidence = field(default_factory=SanitizedTurnEvidence)
    total_seconds: float = 0

    def __post_init__(self):
        if self.arm == "client":
            self.initial.open_target_completed = self.followup.open_target_completed = None


def classify_error(exc: Exception) -> str:
    if isinstance(exc, CodexAuthenticationError):
        return "authentication"
    if isinstance(exc, CodexRateLimitError):
        return "rate_limit"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "http"
    if isinstance(exc, RuntimeError):
        return "runtime"
    return "unexpected"


async def consume_turn(client, arm, evidence, *, model, input_items, tools):
    evidence.started = True
    started = monotonic()
    native = []
    native_unexpected = 0
    tool_unexpected = 0
    stream = client.stream_turn if arm == "client" else client._stream_once
    try:
        async with asyncio.timeout(TURN_SECONDS), aclosing(stream(
            model=model, instructions=INSTRUCTIONS, input_items=input_items, tools=tools,
        )) as deltas:
            async for delta in deltas:
                if isinstance(delta, CodexTextDelta):
                    if delta.text.strip() and evidence.first_visible_text_seconds is None:
                        evidence.first_visible_text_seconds = monotonic() - started
                    evidence.answered |= bool(delta.text.strip())
                elif isinstance(delta, CodexToolCallDelta):
                    tool_unexpected += 1
                    evidence.unexpected_tool_calls = max(native_unexpected, tool_unexpected)
                elif isinstance(delta, CodexResponseItemDelta):
                    native.append(delta.item)
                    evidence.native_items += 1
                    if delta.item.get("type") == "function_call" and not (
                        arm == "client" and tools and delta.item.get("name") == "web_search"
                    ):
                        native_unexpected += 1
                        evidence.unexpected_tool_calls = max(native_unexpected, tool_unexpected)
                    if arm == "builtin" and delta.item.get("type") == "web_search_call":
                        evidence.search_calls += 1
                        if delta.item.get("status") == "completed":
                            evidence.search_completed += 1
                            action = delta.item.get("action")
                            if isinstance(action, dict):
                                evidence.open_target_completed |= (
                                    action.get("type") == "open_page"
                                    and action.get("url") == DIRECT_URL
                                )
        evidence.response_complete = True
        return native
    finally:
        evidence.total_seconds = monotonic() - started


async def run_trial(result, case, model, token, *, transport=None):
    evidence = result.initial

    async def observe_request(request):
        evidence.http_requests += 1
        if request.url.path.endswith("/alpha/search"):
            evidence.search_requests += 1
            opens = json.loads(request.content)["commands"].get("open", [])
            evidence.open_target_requested |= any(
                command.get("ref_id") == DIRECT_URL for command in opens
            )
        else:
            evidence.response_requests += 1

    async def observe_response(response):
        if response.status_code != 200:
            evidence.http_error_responses += 1
        if response.request.url.path.endswith("/alpha/search") and response.status_code == 200:
            await response.aread()
            try:
                payload = response.json()
            except ValueError:
                return
            output = payload.get("output") if isinstance(payload, dict) else None
            if isinstance(output, str) and output.strip():
                evidence.search_usable_responses += 1

    class ObservedClient(CodexClient):
        async def web_search(self, model, arguments, settings):
            evidence.search_calls += 1
            before = evidence.search_requests
            usable_before = evidence.search_usable_responses
            response = await super().web_search(model, arguments, settings)
            successful = (
                evidence.search_requests == before + 1
                and evidence.search_usable_responses == usable_before + 1
                and bool(response.text.strip())
                and not response.text.startswith((
                    "Web search failed", "Web search returned", "Web search found nothing",
                ))
            )
            evidence.search_completed += int(successful)
            return response

    started = monotonic()
    result.status = "running"
    try:
        async with asyncio.timeout(ARM_SECONDS), httpx.AsyncClient(
            transport=transport, timeout=TURN_SECONDS,
            event_hooks={"request": [observe_request], "response": [observe_response]},
        ) as http:
            client = ObservedClient(http_client=http, access_token=token)
            original = [{"role": "user", "content": case.prompt}]
            native = await consume_turn(
                client, result.arm, evidence, model=model,
                input_items=original, tools=[dict(SEARCH_TOOL)],
            )
            if evidence.unexpected_tool_calls:
                result.status = "unexpected_tool"
            elif not evidence.answered or not evidence.native_items:
                result.status = "initial_answer_not_established"
            elif not evidence.search_calls or evidence.search_calls != evidence.search_completed:
                result.status = "search_not_established"
            elif case.requires_open and not (
                evidence.open_target_requested if result.arm == "client"
                else evidence.open_target_completed
            ):
                result.status = "open_not_established"
            else:
                evidence = result.followup
                await consume_turn(
                    client, result.arm, evidence, model=model,
                    input_items=[*original, *native, {"role": "user", "content": FOLLOWUP}],
                    tools=[],
                )
                if evidence.unexpected_tool_calls or evidence.search_calls:
                    result.status = "unexpected_tool"
                elif not evidence.answered:
                    result.status = "followup_answer_not_established"
                else:
                    result.status = "completed"
                    result.gate_pass = True
    except Exception as exc:
        result.status = "error"
        result.error = classify_error(exc)
    finally:
        result.total_seconds = monotonic() - started


async def compare(model, token, first_arm="client", *, transport=None):
    started = monotonic()
    trials = []
    for index, case in enumerate(CASES):
        first = first_arm if index % 2 == 0 else ("builtin" if first_arm == "client" else "client")
        trials.extend(TrialResult(case.name, arm) for arm in (
            first, "builtin" if first == "client" else "client",
        ))
    exit_code = 1
    result = None
    try:
        async with asyncio.timeout(TOTAL_SECONDS):
            for result in trials:
                case = next(case for case in CASES if case.name == result.case)
                await run_trial(result, case, model, token, transport=transport)
                if result.error in {"authentication", "rate_limit"}:
                    exit_code = 2
                    break
                if result.error in {"runtime", "unexpected"}:
                    exit_code = 3
                    break
            else:
                exit_code = 0 if all(trial.gate_pass for trial in trials) else 1
    except TimeoutError:
        exit_code = 3
        if result is not None:
            result.status = "incomplete"
            result.error = "overall_timeout"
    return {
        "model": model, "first_arm": first_arm,
        "total_seconds": monotonic() - started,
        "status": {0: "completed", 1: "failed", 2: "blocked", 3: "incomplete"}[exit_code],
        "live_request_sent": any(
            trial.initial.http_requests or trial.followup.http_requests for trial in trials
        ),
        "trials": [asdict(trial) for trial in trials],
    }, exit_code


def model_identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise argparse.ArgumentTypeError("Use a model identifier, not a URL or free-form text.")
    return value


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({
            "status": "blocked", "error": "invalid_arguments", "live_request_sent": False,
        }))
        raise SystemExit(2)


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=model_identifier)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--first-arm", choices=("client", "builtin"), default=None)
    parser.add_argument("--builtin-followup-only", action="store_true")
    args = parser.parse_args()
    if args.builtin_followup_only and args.first_arm is not None:
        parser.error("The diagnostic mode has a fixed arm.")
    first_arm = args.first_arm or "client"
    if args.dry_run:
        report = {
            "model": args.model, "first_arm": first_arm, "status": "dry_run",
            "cases": [case.name for case in CASES], "live_request_sent": False,
            "max_arms": 6, "max_turns": 12, "max_http_requests": 36,
            "client_max_http_requests_per_arm": 10, "builtin_max_http_requests_per_arm": 2,
            "turn_seconds": TURN_SECONDS, "arm_seconds": ARM_SECONDS,
            "total_seconds": TOTAL_SECONDS,
        }
        if args.builtin_followup_only:
            report = {
                "mode": "builtin_followup_only", "model": args.model, "status": "dry_run",
                "cases": [CASES[0].name], "live_request_sent": False,
                "max_arms": 1, "max_turns": 2, "max_http_requests": 2,
                "turn_seconds": TURN_SECONDS, "total_seconds": ARM_SECONDS,
            }
        code = 0
    else:
        token = os.environ.get("CODEX_ASSIST_ACCESS_TOKEN", "").strip()
        if not token:
            report = {
                "status": "blocked", "error": "missing_credentials", "live_request_sent": False,
            }
            code = 2
        else:
            if args.builtin_followup_only:
                from scripts.search_stream_diagnostic import diagnose

                report, code = asyncio.run(diagnose(args.model, token))
            else:
                report, code = asyncio.run(compare(args.model, token, first_arm))
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
