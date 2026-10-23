"""Exhaustive interleaving check for refresh-token rotation.

Drives the real ``RuntimeTokenCoordinator`` coroutines by hand, so every point
where one suspends is a scheduling choice, and runs every ordering of those
choices for small bounded scenarios. It checks that a rotating refresh token is
never spent twice and that the stored token is still usable afterwards.
Maintained through ``.agents/skills/verify-token-rotation/SKILL.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Coroutine
from dataclasses import dataclass

import pytest

from custom_components.codex_assist.codex_auth import (
    CodexAuthTemporaryError,
    CodexReauthRequiredError,
    CodexTokenSet,
)
from custom_components.codex_assist.codex_runtime import (
    ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    runtime_token_coordinator,
)
from tests.ha_fakes import FakeConfigEntry

FAR_FUTURE = 4_000_000_000


def _jwt(exp: float, tag: str) -> str:
    payload = json.dumps({"exp": exp, "jti": tag}).encode()
    return f"header.{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.sig"


def _exp(access_token: str) -> float:
    segment = access_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))["exp"]


@dataclass
class Gate:
    """One await the explorer resolves: an auth-server or Codex backend answer."""

    actor: str
    what: str
    outcomes: tuple[str, ...]
    future: asyncio.Future


@dataclass
class Actor:
    name: str
    coro: Coroutine
    started: bool = False
    waiting: asyncio.Future | None = None
    done: bool = False
    error: BaseException | None = None

    def blocked_in(self) -> list[str]:
        frames, coro = [], self.coro
        while hasattr(coro, "cr_code"):
            frames.append(coro.cr_code.co_qualname)
            coro = coro.cr_await
        return frames


class AuthServer:
    """OAuth server with rotating refresh tokens; reuse of a spent token is rejected."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.issued = 0
        self.families = 0
        self.current: dict[int, str] = {}
        self.family_of: dict[str, int] = {}

    def sign_in(self, access_exp: float) -> CodexTokenSet:
        self.families += 1
        return self._issue(self.families, access_exp)

    def _issue(self, family: int, access_exp: float) -> CodexTokenSet:
        self.issued += 1
        refresh = f"R{self.issued}"
        self.current[family] = refresh
        self.family_of[refresh] = family
        return CodexTokenSet(_jwt(access_exp, f"A{self.issued}"), refresh)

    def client(self, actor: str) -> ActorAuthClient:
        return ActorAuthClient(self, actor)


class ActorAuthClient:
    def __init__(self, server: AuthServer, actor: str) -> None:
        self.server = server
        self.actor = actor

    async def refresh(self, tokens: CodexTokenSet) -> CodexTokenSet:
        world = self.server.world
        sent = tokens.refresh_token
        rejected = world.rejected.get(self.actor)
        if rejected is not None:
            needed = sent == rejected.refresh_token
        else:
            needed = _exp(tokens.access_token) <= time.time() + ACCESS_TOKEN_REFRESH_SKEW_SECONDS
        if not needed:
            world.violate(f"NoRedundantRefresh: {self.actor} refreshed {sent} without needing to")
        outcome = await world.gate(self.actor, f"refresh({sent})", ("ok", "temporary"))
        if outcome == "temporary":
            world.fell_back.add(self.actor)
            raise CodexAuthTemporaryError("synthetic 503")
        family = self.server.family_of[sent]
        if self.server.current[family] != sent:
            world.violate(f"NoRefreshReuse: auth server received spent refresh token {sent}")
            raise CodexReauthRequiredError("refresh_token_reused")
        rotated = self.server._issue(family, FAR_FUTURE)
        world.note(f"{self.actor}: auth server rotated {sent} -> {rotated.refresh_token}")
        return rotated


class World:
    def __init__(self, scenario: Scenario) -> None:
        self.loop = asyncio.get_running_loop()
        self.scenario = scenario
        self.trace: list[str] = []
        self.violations: list[str] = []
        self.gates: dict[asyncio.Future, Gate] = {}
        self.coordinators: dict[int, int] = {}
        self.rejected: dict[str, CodexTokenSet] = {}
        self.fell_back: set[str] = set()
        self.interrupted = False
        self.server = AuthServer(self)
        tokens = self.server.sign_in(time.time() + 60)  # inside the 120 s refresh skew
        self.entry = FakeConfigEntry(
            data={
                "model": "m",
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
            }
        )
        self.actors = [
            Actor(name, ACTORS[name.split("-")[0]](self, name)) for name in scenario.actors
        ]
        self.events = [step for event in scenario.events for step in EVENT_STEPS[event]]

    def coordinator_name(self, coordinator) -> str:
        number = self.coordinators.setdefault(id(coordinator), len(self.coordinators) + 1)
        return f"coordinator#{number}"

    def note(self, line: str) -> None:
        self.trace.append(line)

    def violate(self, message: str) -> None:
        self.violations.append(message)

    def update_entry(self, data: dict) -> None:
        # hass.config_entries.async_update_entry(entry, data=data) is synchronous.
        self.entry.data = dict(data)
        self.note(f"  entry.data refresh_token := {data['refresh_token']}")

    async def gate(self, actor: str, what: str, outcomes: tuple[str, ...]) -> str:
        gate = Gate(actor, what, outcomes, self.loop.create_future())
        self.gates[gate.future] = gate
        self.note(f"{actor}: sends {what}")
        return await gate.future

    async def backend(self, actor: str, tokens: CodexTokenSet, *, last: bool = True) -> str:
        expired = _exp(tokens.access_token) <= time.time()
        outcomes = ("401",) if expired else ("done", "401") if last else ("done", "tool", "401")
        return await self.gate(actor, "Codex request", outcomes)

    def choices(self) -> list[tuple[str, object]]:
        found: list[tuple[str, object]] = []
        for actor in self.actors:
            if actor.done:
                continue
            gate = self.gates.get(actor.waiting)
            if gate is not None:
                for outcome in gate.outcomes:
                    label = f"{actor.name}: {gate.what} answered {outcome}"
                    found.append((label, ("answer", actor, gate, outcome)))
            elif actor.waiting is None or actor.waiting.done():
                label = f"{actor.name}: resumes" if actor.started else f"start {actor.name}"
                found.append((label, ("step", actor)))
        if self.events:
            found.append((self.events[0][0], ("event", self.events[0])))
        return found

    def apply(self, label: str, choice) -> None:
        kind = choice[0]
        if kind == "answer":
            # The answer and the resumption are one step: the auth server's
            # effect runs inside the actor, so splitting them adds no ordering.
            _, actor, gate, outcome = choice
            del self.gates[gate.future]
            self.note(label)
            gate.future.set_result(outcome)
            self.step(actor)
        elif kind == "step":
            self.note(label)
            self.step(choice[1])
        else:
            self.events.pop(0)
            self.note(f"HA: {label}")
            choice[1][1](self)

    def step(self, actor: Actor) -> None:
        actor.started = True
        try:
            actor.waiting = actor.coro.send(None)
        except StopIteration:
            actor.done = True
            return
        except (Exception, asyncio.CancelledError) as err:
            actor.done, actor.error = True, err
            return
        if actor.waiting is not None and not isinstance(actor.waiting, asyncio.Future):
            self.violate(f"NoUnsupportedAwait: {actor.name} yielded {actor.waiting!r}")
            actor.done = self.interrupted = True

    def check_final(self) -> None:
        stuck = [actor for actor in self.actors if not actor.done]
        # Only gates, lock handoffs and bare yields can be resumed here. A
        # future that needs the event loop (a task, shield, sleep(>0)) never is.
        unsupported = [a for a in stuck if "Lock.acquire" not in a.blocked_in()]
        for actor in unsupported:
            self.violate(
                f"NoUnsupportedAwait: {actor.name} waits in {actor.blocked_in()[-1]} on "
                f"{actor.waiting!r}, which only the event loop can complete"
            )
        if stuck and not unsupported and not self.interrupted:
            self.violate(f"NoDeadlock: {[actor.name for actor in stuck]} never finished")
        for actor in self.actors:
            actor.coro.close()
            if actor.error is not None:
                self.violate(f"NoCrash: {actor.name} raised {actor.error!r}")
        if stuck or self.interrupted:
            return
        stored = self.entry.data["refresh_token"]
        family = self.server.family_of[stored]
        if family != self.server.families:
            self.violate(f"StoredTokenUsable: entry holds {stored} from an older sign-in")
        elif self.server.current[family] != stored:
            self.violate(
                f"StoredTokenUsable: entry holds spent {stored}; "
                f"auth server's live token is {self.server.current[family]}"
            )


async def _call(world: World, actor: str, coordinator, method: str, **kwargs):
    world.note(f"{actor}: {method}() on {world.coordinator_name(coordinator)}")
    world.fell_back.discard(actor)
    if "rejected_tokens" in kwargs:
        world.rejected[actor] = kwargs["rejected_tokens"]
    tokens = await getattr(coordinator, method)(
        lambda: world.entry.data,
        auth_client=world.server.client(actor),
        async_update_entry_data=world.update_entry,
        **kwargs,
    )
    stored = (world.entry.data["access_token"], world.entry.data["refresh_token"])
    # After a temporary refresh failure, resolve_runtime_tokens deliberately returns
    # the still-valid tokens it read, even if a reauth has stored newer ones.
    if actor not in world.fell_back and (tokens.access_token, tokens.refresh_token) != stored:
        world.violate(f"ReturnsStoredTokens: {method}() gave {actor} tokens not in entry.data")
    return tokens


async def request_actor(world: World, actor: str) -> None:
    """Conversation and AI Task: look up the coordinator at each call.

    Each tool round sends a Codex request. A 401 calls _refresh_runtime_tokens
    and retries once, and a second 401 starts reauth (conversation.py
    async_process loop, ai_task.py run_tool_round). The last retry is left out:
    its answer never reaches the coordinator.
    """
    try:
        tokens = await _call(world, actor, runtime_token_coordinator(world.entry), "resolve")
        rounds = world.scenario.tool_rounds
        for round_number in range(rounds):
            last = round_number == rounds - 1
            answer = await world.backend(actor, tokens, last=last)
            if answer == "401":
                tokens = await _call(
                    world,
                    actor,
                    runtime_token_coordinator(world.entry),
                    "refresh_after_rejection",
                    rejected_tokens=tokens,
                )
                if last:
                    return
                answer = await world.backend(actor, tokens, last=last)
                if answer == "401":
                    world.note(f"{actor}: starts reauth after the retry was rejected")
                    return
            if answer == "done":
                return
    except (CodexReauthRequiredError, CodexAuthTemporaryError) as err:
        world.note(f"{actor}: gives up with {type(err).__name__}")


async def discovery_actor(world: World, actor: str) -> None:
    """model_discovery.async_entry_model_catalog: coordinator captured once at the start."""
    coordinator = runtime_token_coordinator(world.entry)
    try:
        tokens = await _call(world, actor, coordinator, "resolve")
        if await world.backend(actor, tokens) == "done":
            return
        tokens = await _call(
            world, actor, coordinator, "refresh_after_rejection", rejected_tokens=tokens
        )
    except (CodexReauthRequiredError, CodexAuthTemporaryError) as err:
        world.note(f"{actor}: gives up with {type(err).__name__}")


def _reauth_writes_new_sign_in(world: World) -> None:
    # config_flow.py: async_update_reload_and_abort(entry, data_updates=...) after device sign-in.
    tokens = world.server.sign_in(FAR_FUTURE)
    world.update_entry(
        {
            **world.entry.data,
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
        }
    )


def _unload(world: World) -> None:
    # homeassistant/config_entries.py ConfigEntry.async_unload deletes runtime_data.
    if hasattr(world.entry, "runtime_data"):
        del world.entry.runtime_data


ACTORS = {"conversation": request_actor, "ai_task": request_actor, "discovery": discovery_actor}
EVENT_STEPS = {
    "reload": [("reload: unload entry", _unload)],
    "reauth": [
        ("reauth: write new sign-in to entry.data", _reauth_writes_new_sign_in),
        ("reauth reload: unload entry", _unload),
    ],
}


@dataclass(frozen=True)
class Scenario:
    actors: tuple[str, ...]
    events: tuple[str, ...] = ()
    tool_rounds: int = 1


async def _run(scenario: Scenario, forced: list[int]) -> tuple[World, list[int]]:
    world = World(scenario)
    widths: list[int] = []
    while True:
        options = world.choices()
        if not options:
            break
        index = forced[len(widths)] if len(widths) < len(forced) else 0
        widths.append(len(options))
        world.apply(*options[index])
    world.check_final()
    return world, widths


async def explore(scenario: Scenario) -> tuple[int, World | None]:
    """Depth-first over every schedule; return (schedules run, first failing world)."""
    stack: list[list[int]] = []
    runs = 0
    while True:
        world, widths = await _run(scenario, [index for index, _ in stack])
        runs += 1
        if world.violations:
            return runs, world
        stack += [[0, width] for width in widths[len(stack) :]]
        while stack and stack[-1][0] + 1 >= stack[-1][1]:
            stack.pop()
        if not stack:
            return runs, None
        stack[-1][0] += 1


def _report(world: World) -> str:
    return "\n".join(["violations:", *world.violations, "counterexample:", *world.trace])


VERIFIED = {
    "two_requests_share_one_refresh": Scenario(("conversation-1", "ai_task-1")),
    "request_and_discovery_share_one_refresh": Scenario(("conversation-1", "discovery-1")),
    "request_across_reauth": Scenario(("conversation-1",), ("reauth",)),
    "discovery_across_reauth": Scenario(("discovery-1",), ("reauth",)),
    "requests_across_plain_reload": Scenario(("conversation-1", "ai_task-1"), ("reload",)),
    "request_and_discovery_across_reauth": Scenario(
        ("conversation-1", "discovery-1"), ("reauth",)
    ),
    "tool_rounds_and_discovery_share_refreshes": Scenario(
        ("conversation-1", "discovery-1"), tool_rounds=2
    ),
    "tool_rounds_across_reauth": Scenario(("conversation-1",), ("reauth",), tool_rounds=2),
    "two_requests_with_tool_rounds": Scenario(("conversation-1", "ai_task-1"), tool_rounds=2),
}


@pytest.mark.parametrize("name", sorted(VERIFIED))
async def test_rotation_holds_for_every_interleaving(name: str) -> None:
    runs, failing = await explore(VERIFIED[name])
    assert failing is None, _report(failing)
    print(f"{name}: {runs} schedules")
