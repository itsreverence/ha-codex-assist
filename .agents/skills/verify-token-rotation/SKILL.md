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

The run takes about 2 s. A healthy run reports `6 passed`, and `-s` prints the schedule count for each scenario. A failure prints the counterexample.

The tool is a stateless model checker written in the test file. It runs the **real** `RuntimeTokenCoordinator` and replays the scenario from scratch, depth-first, once for every ordering. Every await the coordinator makes on the outside world is a `Gate`: an auth-server answer (`ok` / `temporary`) or a Codex backend answer (`ok` / `401`). The explorer chooses which gate resolves next, with which outcome, and when each actor starts or each HA event step runs. asyncio is single-threaded, so code between two awaits is one indivisible step in the product too. `_settle` runs everything until all coroutines are blocked; it reads CPython's `loop._ready`. There are no other dependencies.

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

**Sync commit:** `01f0b8839c779cc6308fa31ce89b72f0773155fe`. At that commit, the actors and events above matched the code.

To see whether the covered code changed since then:

```bash
git diff 01f0b88 -- custom_components/codex_assist/codex_runtime.py custom_components/codex_assist/conversation.py custom_components/codex_assist/ai_task.py custom_components/codex_assist/model_discovery.py custom_components/codex_assist/config_flow.py custom_components/codex_assist/__init__.py custom_components/codex_assist/codex_auth.py
```

For a change in `codex_runtime.py`, just rerun. The real code is under test. For a change in a caller, reload/reauth, or the auth client, first update the matching actor or event so that it still does what the code does. That includes the order of lookups and awaits and whether the coordinator is captured or looked up again. Then rerun. Move the sync commit forward only after you have confirmed that every row of the table describes the code at the new commit.

## Scenarios and bounds

`VERIFIED` lists scenarios that must pass for every schedule:

- `two_requests_share_one_refresh`: Conversation and AI Task (426 schedules)
- `request_and_discovery_share_one_refresh`: Conversation and discovery (426 schedules)
- `request_across_reauth`: Conversation while a reauth writes a new sign-in and reloads (50 schedules)
- `discovery_across_reauth`: discovery while the same reauth runs (50 schedules)
- `requests_across_plain_reload`: Conversation and AI Task around a reload with unchanged tokens, such as a manual reload (3,332 schedules)
- `request_and_discovery_across_reauth`: Conversation and discovery while a reauth reloads (6,272 schedules)

Each actor stops after its last coordinator call. The retried Codex request that follows never reaches the coordinator, so the check leaves it out.

Assumptions:

- The stored access token starts inside the 120 s refresh skew but has not expired.
- Refreshed and reauth tokens never expire.
- The backend may return 401 for any token, which models opaque tokens and server revocation, and returns only 401 for an expired one.
- Each actor retries at most once after any 401. The code retries only when `CodexAuthenticationError.retry_allowed` is set, so the check explores a superset of its schedules.
- Each scenario has at most two actors and at most one reload or reauth.

Not covered:

- more actors or repeated events
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

`runtime_token_coordinator` now keeps one coordinator per entry object in a `weakref.WeakKeyDictionary`, so a reload keeps the lock and a removed entry frees it. If a change moves the coordinator back into `runtime_data` or `hass.data` cleared on unload, `requests_across_plain_reload` fails.

## Real bug or modeling error?

Read the printed counterexample one step at a time against the code.

- If every step is something the code and HA can really do, it is a bug. Report it in those terms. Do not weaken the property or remove the scenario.
- If a step is something the code cannot do, the harness is wrong. Examples: an actor looks up the coordinator where the caller captures it, an event happens that HA never triggers, or the auth fake answers differently from `CodexAuthClient.refresh`. Fix the actor or event and cite the line it mirrors.

`NoDeadlock` and `NoCrash` violations usually mean the harness is broken, for example a new await the explorer cannot resolve or an exception type the actors don't catch. They are rarely product bugs.

## Proof that it can fail

Each of these guards was removed from `codex_runtime.py` once, and the check was run against the result at `01f0b88`:

| Mutation | Caught by |
|---|---|
| `resolve` without `async with self._refresh_lock` | NoRefreshReuse |
| `refresh_after_rejection` without the lock | NoRefreshReuse |
| `resolve_runtime_tokens` drops `if latest != tokens: return latest` | StoredTokenUsable |
| `refresh_after_rejection` drops `if latest != current: return latest` | StoredTokenUsable |
| `refresh_after_rejection` drops `if current != rejected_tokens: return current` | NoRedundantRefresh |

When you change the harness, repeat all five mutations, then restore the file with `git checkout -- custom_components/codex_assist/codex_runtime.py`.
