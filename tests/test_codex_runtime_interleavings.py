"""Exhaustive interleaving check for refresh-token rotation.

Runs the real ``RuntimeTokenCoordinator`` under every ordering of its await
points for small bounded scenarios and checks that a rotating refresh token is
never spent twice and that the stored token is still usable afterwards.
Maintained through ``.agents/skills/verify-token-rotation/SKILL.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
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


class Entry:
    """Config entry: HA replaces ``data`` on update and deletes ``runtime_data`` on unload."""

    def __init__(self, data: dict) -> None:
        self.data = data


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
        self.gates: list[Gate] = []
        self.coordinators: dict[int, int] = {}
        self.rejected: dict[str, CodexTokenSet] = {}
        self.fell_back: set[str] = set()
        self.server = AuthServer(self)
        tokens = self.server.sign_in(time.time() + 60)  # inside the 120 s refresh skew
        self.entry = Entry(
            {
                "model": "m",
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
            }
        )
        self.coordinator_name(runtime_token_coordinator(self.entry))  # __init__.py:22
        self.unstarted = list(scenario.actors)
        self.tasks: dict[str, asyncio.Task] = {}
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
        self.gates.append(gate)
        self.note(f"{actor}: sends {what}")
        return await gate.future

    async def backend(self, actor: str, tokens: CodexTokenSet) -> str:
        expired = _exp(tokens.access_token) <= time.time()
        outcomes = ("401",) if expired else ("ok", "401")
        return await self.gate(actor, "Codex request", outcomes)

    def choices(self) -> list[tuple[str, object]]:
        found: list[tuple[str, object]] = []
        for name in self.unstarted:
            found.append((f"start {name}", ("start", name)))
        for gate in self.gates:
            for outcome in gate.outcomes:
                label = f"{gate.actor}: {gate.what} answered {outcome}"
                found.append((label, ("gate", gate, outcome)))
        if self.events:
            found.append((self.events[0][0], ("event", self.events[0])))
        return found

    def apply(self, label: str, choice) -> None:
        kind = choice[0]
        if kind == "start":
            name = choice[1]
            self.unstarted.remove(name)
            self.note(f"{name}: starts")
            self.tasks[name] = self.loop.create_task(ACTORS[name.split("-")[0]](self, name))
        elif kind == "gate":
            _, gate, outcome = choice
            self.gates.remove(gate)
            self.note(label)
            gate.future.set_result(outcome)
        else:
            self.events.pop(0)
            self.note(f"HA: {label}")
            choice[1][1](self)

    def check_final(self) -> None:
        stuck = [name for name, task in self.tasks.items() if not task.done()]
        if stuck:
            self.violate(f"NoDeadlock: {stuck} never finished")
        for name, task in self.tasks.items():
            if task.done() and task.exception() is not None:
                self.violate(f"NoCrash: {name} raised {task.exception()!r}")
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

    conversation.py resolve() then _refresh_runtime_tokens() after a 401;
    ai_task.py follows the same shape.
    """
    try:
        tokens = await _call(world, actor, runtime_token_coordinator(world.entry), "resolve")
        if await world.backend(actor, tokens) == "ok":
            return
        tokens = await _call(
            world,
            actor,
            runtime_token_coordinator(world.entry),
            "refresh_after_rejection",
            rejected_tokens=tokens,
        )
        await world.backend(actor, tokens)
    except (CodexReauthRequiredError, CodexAuthTemporaryError) as err:
        world.note(f"{actor}: gives up with {type(err).__name__}")


async def discovery_actor(world: World, actor: str) -> None:
    """model_discovery.async_entry_model_catalog: coordinator captured once at the start."""
    coordinator = runtime_token_coordinator(world.entry)
    try:
        tokens = await _call(world, actor, coordinator, "resolve")
        if await world.backend(actor, tokens) == "ok":
            return
        tokens = await _call(
            world, actor, coordinator, "refresh_after_rejection", rejected_tokens=tokens
        )
        await world.backend(actor, tokens)
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


def _setup(world: World) -> None:
    world.note(f"  setup uses {world.coordinator_name(runtime_token_coordinator(world.entry))}")


ACTORS = {"conversation": request_actor, "ai_task": request_actor, "discovery": discovery_actor}
EVENT_STEPS = {
    "reload": [("reload: unload entry", _unload), ("reload: async_setup_entry", _setup)],
    "reauth": [
        ("reauth: write new sign-in to entry.data", _reauth_writes_new_sign_in),
        ("reauth reload: unload entry", _unload),
        ("reauth reload: async_setup_entry", _setup),
    ],
}


@dataclass(frozen=True)
class Scenario:
    actors: tuple[str, ...]
    events: tuple[str, ...] = ()


async def _settle(loop: asyncio.AbstractEventLoop) -> None:
    # Run every coroutine until it blocks on a Gate or a lock held by one.
    # _ready is CPython's run queue; empty on resumption means nothing else can run.
    idle = 0
    while idle < 2:
        await asyncio.sleep(0)
        idle = idle + 1 if not loop._ready else 0  # noqa: SLF001


async def _run(scenario: Scenario, forced: list[int]) -> tuple[World, list[int]]:
    world = World(scenario)
    widths: list[int] = []
    while True:
        await _settle(world.loop)
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
}

# Each reload deletes entry.runtime_data, so requests still in flight hold the old
# coordinator lock while new requests take a fresh one. Remove a scenario from here
# when the coordinator outlives reloads; strict xfail flags that change.
KNOWN_FINDINGS = {
    "requests_across_plain_reload": Scenario(("conversation-1", "ai_task-1"), ("reload",)),
    "request_and_discovery_across_reauth": Scenario(
        ("conversation-1", "discovery-1"), ("reauth",)
    ),
}


@pytest.mark.parametrize("name", sorted(VERIFIED))
async def test_rotation_holds_for_every_interleaving(name: str) -> None:
    runs, failing = await explore(VERIFIED[name])
    assert failing is None, _report(failing)
    print(f"{name}: {runs} schedules")


@pytest.mark.xfail(
    strict=True,
    reason="Finding: a reload replaces the coordinator lock while a refresh is in "
    "flight, so a second refresh can spend the same rotating token.",
)
@pytest.mark.parametrize("name", sorted(KNOWN_FINDINGS))
async def test_known_finding_still_reproduces(name: str) -> None:
    _, failing = await explore(KNOWN_FINDINGS[name])
    assert failing is None, _report(failing)
