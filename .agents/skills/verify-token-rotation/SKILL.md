---
name: verify-token-rotation
description: Exhaustively check Codex Assist's refresh-token rotation (RuntimeTokenCoordinator, its callers in conversation, AI Task and model discovery, and the reauth/reload path) under every interleaving of its await points. Use when a change touches codex_runtime.py, how callers resolve or refresh tokens, config-entry reload or reauth, or entry.runtime_data.
---

# Verify refresh-token rotation

## The promise

**Property.** Under every ordering of concurrent Conversation, AI Task, and model-discovery requests, reauth, and reload, Codex Assist never sends the auth server a refresh token that it already rotated, and when the requests finish, the entry still holds the server's live refresh token from the latest sign-in.

**Harm if broken.** OpenAI rotates refresh tokens. Reusing a spent one gets a 400/401, and that becomes `CodexReauthRequiredError`. The user is signed out and must redo device-code sign-in. A stale stored token causes the same sign-out on the next refresh.

The check enforces four properties. The first two are the promise above; the last two are the rule that concurrent requests reuse the winning refresh.

- `NoRefreshReuse`: the auth server never receives a spent refresh token.
- `StoredTokenUsable`: when the requests finish, `entry.data` holds the live refresh token of the latest sign-in.
- `NoRedundantRefresh`: `resolve()` refreshes only an expiring access token, and `refresh_after_rejection()` refreshes only the token that was rejected.
- `ReturnsStoredTokens`: the coordinator hands a caller exactly the tokens in `entry.data`. One exception is deliberate: after a temporary refresh error, `resolve_runtime_tokens` returns the still-valid tokens it read, even if a reauth has stored newer ones (`codex_runtime.py`, the `except CodexAuthTemporaryError` branch).

These properties replace the four hand-ordered concurrency tests that used to live in `tests/test_codex_runtime.py`. The single-call unit tests there remain.

## Run it

```bash
uv run pytest tests/test_codex_runtime_interleavings.py -q -s
```

The run takes about 6 s. A healthy run reports `9 passed`, and `-s` prints the schedule count for each scenario. A failure prints the counterexample.

The tool is a stateless model checker written in the test file. It drives the **real** `RuntimeTokenCoordinator` coroutines by hand with `send` and replays each scenario from scratch, depth-first, once for every ordering. Every point where an actor suspends is a choice:

- An await on a `Gate` (an auth-server answer `ok` / `temporary`, or a Codex backend answer `ok` / `401`) offers one choice per outcome. The answer and the actor's resumption are one step, because the gate's effect runs inside the actor.
- An await on a lock held by another actor, or a bare `yield` such as `asyncio.sleep(0)`, makes the actor runnable, and resuming it is a separate choice.
- Each HA event step is a choice.

Code between two suspensions runs as one step, which matches asyncio. A new `await` on a gate, an `asyncio.Lock` or a bare yield becomes a new scheduling point without any harness change. The explorer never yields to the event loop, so it cannot resume an await on anything only the loop completes: a task, `asyncio.shield`, `ensure_future`, `sleep` with a delay, `to_thread`, or an executor job. Those runs report `NoStuckActor`, the same violation as a real deadlock, so read the frame and the future it names to tell them apart (see below). An actor that yields something other than a future or `None` reports `NoUnsupportedAwait`. Actors also run inside the test's own task, so `asyncio.current_task()`, context variables and `asyncio.timeout` see the test task: a timeout around a coordinator await is never explored. There are no other dependencies.

## Does my change touch this?

Yes, if it changes any of these:

| Covered code | What the check relies on |
|---|---|
| `custom_components/codex_assist/codex_runtime.py`: `RuntimeTokenCoordinator.resolve`, `.refresh_after_rejection`, `runtime_token_coordinator` (one coordinator per entry object, kept in `_COORDINATORS` across reloads), `resolve_runtime_tokens`, `_persist_runtime_tokens` | Runs as is. Nothing is modeled. |
| `conversation.py`: `resolve(...)` in `async_process`, `_refresh_runtime_tokens` | `request_actor`: coordinator looked up at each call, one retry after a 401 |
| `ai_task.py`: both `resolve(...)` calls and the 401 retry | `request_actor` (same shape) |
| `model_discovery.py` `async_entry_model_catalog` | `discovery_actor`: coordinator captured **once** at the start |
| `config_flow.py` reauth/reconfigure `async_update_reload_and_abort` | `_reauth_writes_new_sign_in`, then `_unload` |
| `__init__.py` `async_setup_entry` does not look up the coordinator; callers create it on first use | No setup step. If setup starts creating or replacing the coordinator, add a step after `_unload`. |
| HA `ConfigEntry.async_unload` deleting `runtime_data` (HA 2026.10 `config_entries.py:1138`) | `_unload`. The coordinator no longer lives there, so this must not reset it. |
| `hass.config_entries.async_update_entry` (synchronous, replaces `entry.data`) | `World.update_entry` |
| `codex_auth.CodexAuthClient.refresh` error mapping | `ActorAuthClient.refresh`: reuse raises `CodexReauthRequiredError`, `temporary` raises `CodexAuthTemporaryError` |

**Sync point:** the last commit that changed this file. At that commit, the actors and events above matched the code.

To see whether the covered callers changed since then, run this from the repository root. It refuses to run in a shallow clone, where the anchor would be wrong.

```bash
test "$(git rev-parse --is-shallow-repository)" = false && git diff "$(git log -1 --format=%H -- .agents/skills/verify-token-rotation/SKILL.md)" -- custom_components/codex_assist/conversation.py custom_components/codex_assist/ai_task.py custom_components/codex_assist/model_discovery.py custom_components/codex_assist/config_flow.py custom_components/codex_assist/__init__.py custom_components/codex_assist/codex_auth.py
```

For a change in `codex_runtime.py`, just rerun. The real code is under test, and new awaits are scheduled automatically. For a change in a caller, reload/reauth, or the auth client, first update the matching actor or event so that it still does what the code does. That includes the order of lookups and awaits and whether the coordinator is captured or looked up again. Then rerun. Any commit to this file moves the sync point, so commit an edit here only after you have confirmed that every row of the table describes the code in that commit.

## Scenarios and bounds

`VERIFIED` lists scenarios that must pass for every schedule:

- `two_requests_share_one_refresh`: Conversation and AI Task (548 schedules)
- `request_and_discovery_share_one_refresh`: Conversation and discovery (548 schedules)
- `request_across_reauth`: Conversation while a reauth writes a new sign-in and reloads (50 schedules)
- `discovery_across_reauth`: discovery while the same reauth runs (50 schedules)
- `requests_across_plain_reload`: Conversation and AI Task around a reload with unchanged tokens, such as a manual reload (4,872 schedules)
- `request_and_discovery_across_reauth`: Conversation and discovery while a reauth reloads (9,600 schedules)
- `tool_rounds_and_discovery_share_refreshes`: Conversation with two tool rounds, each able to refresh once, and discovery (3,558 schedules)
- `tool_rounds_across_reauth`: Conversation with two tool rounds while a reauth reloads (310 schedules)
- `two_requests_with_tool_rounds`: Conversation and AI Task, each with two tool rounds (39,106 schedules)

A request actor runs `tool_rounds` rounds, one by default. Each round sends a Codex request, and a 401 refreshes and retries once, as the tool-round loops in `conversation.py` and `ai_task.py` do. A second 401 ends the actor, which models the reauth start. The retry in the last round, and discovery's retry, are left out because their answers never reach the coordinator.

`tests_ha/test_smoke.py::test_token_coordinator_survives_reload_but_not_removal` pins the fact the fix rests on against real Home Assistant: `async_reload` keeps the same `ConfigEntry` object, so it gets the same coordinator, and a removed and re-added entry gets a new one. The model only assumes this.

Assumptions:

- The stored access token starts inside the 120 s refresh skew but has not expired.
- Refreshed and reauth tokens never expire.
- The backend may return 401 for any token, which models opaque tokens and server revocation, and returns only 401 for an expired one.
- Any 401 may trigger the refresh and retry. The code retries only when `CodexAuthenticationError.retry_allowed` is set, so the actors allow more retries than the code does.
- Each scenario has at most two actors, at most one reload or reauth, and at most two tool rounds per request actor. The code allows up to six rounds (`MAX_TOOL_ITERATIONS + 1`).

Not covered:

- more actors, more tool rounds, or repeated events
- a refresh the server applied whose response was lost, which no client-side lock can fix
- clock movement during a run
- HA cancelling tasks on shutdown
- model-cache coalescing in `ModelDiscoveryCache`
- the token values the real HTTP auth server returns

## The reload finding (fixed)

The coordinator used to live in `entry.runtime_data`, which HA deletes on every unload. Requests still in flight kept the old lock while new requests took a fresh one. The check found this sequence:

1. `conversation-1` calls `resolve()` on coordinator#1 and sends `refresh(R1)`.
2. HA unloads the entry, deleting `runtime_data`.
3. `ai_task-1` calls `resolve()` and gets coordinator#2, whose lock is free. The stored token is still R1 and expiring, so it sends `refresh(R1)`.
4. The server rotates R1 for `conversation-1`, rejects R1 from `ai_task-1` as reused, and `ai_task-1` starts reauth.

`runtime_token_coordinator` now keeps one coordinator per entry object in a `weakref.WeakKeyDictionary`, so a reload keeps the lock, and a removed and re-added entry gets a new one. If a change moves the coordinator back into `runtime_data`, `requests_across_plain_reload` fails. The model has no `hass.data` and does not run `async_unload_entry`, so a store that unload clears some other way would pass the model. `test_token_coordinator_survives_reload_but_not_removal` catches that case in real HA.

## Real bug or modeling error?

Read the printed counterexample one step at a time against the code.

- If every step is something the code and HA can really do, it is a bug. Report it in those terms. Do not weaken the property or remove the scenario.
- If a step is something the code cannot do, the harness is wrong. Examples: an actor looks up the coordinator where the caller captures it, an event happens that HA never triggers, or the auth fake answers differently from `CodexAuthClient.refresh`. Fix the actor or event and cite the line it mirrors.

`NoUnsupportedAwait` and `NoCrash` usually mean the harness is broken, for example an awaitable the explorer cannot drive or an exception type the actors don't catch. `NoStuckActor` can be either. Actors in `Lock.acquire` are usually queued behind another stuck actor, so start with the one that is not. Then ask who completes the future it waits on. If an actor should, and no actor is left to do it, it is a product deadlock. That covers a `Lock`, `Event` or `Semaphore`, and a future the coordinator creates for another actor to set, which shows as a bare `<Future pending>`. If only the event loop completes it, the harness needs extending. That shows as `<Task pending ...>`, a future with `cb=[shield...]`, or a timer or executor future. For a bare `<Future pending>`, find its setter in the code before deciding. If every stuck actor is in `Lock.acquire`, it is a product deadlock. When an actor is stuck, the run skips the final-state checks, so a stuck run reports no `StoredTokenUsable`.

## Proof that it can fail

`scripts/check_mutations.py` holds six mutations of `codex_runtime.py`, each with the property that must catch it. The script copies the checkout to a temporary directory and never writes to the checkout itself. It runs the check unmutated first, then applies each mutation in the copy and runs the check again. CI runs it on every pull request.

```bash
uv run python scripts/check_mutations.py --suite fast
```

When you change `codex_runtime.py` or the harness, rerun the script. Every mutation must print `caught`.

- `SURVIVED (test passed)` means the check passed with the mutation applied. If the anchored code is now dead, or the guard is also implemented elsewhere, re-anchor. Otherwise the harness or its properties regressed. Fix that. Do not delete the entry.
- `WRONG FAILURE (expected text not found)` means the check failed with a different violation. Read the printed counterexample. Change `expect` only if that violation proves the same guard matters.
- `anchor missing` or `anchor ambiguous (N matches)` means a code change removed the text the mutation replaces, or made it occur more than once. Re-anchor the mutation to the code that now implements the contract. Do not delete or weaken the entry.
