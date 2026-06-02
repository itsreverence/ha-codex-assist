# Codex Assist Device-Code OAuth Flow Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task if routing to workers. For direct work, keep TDD and verify with `uv run pytest -q` plus `uv run ruff check .`.

**Goal:** Add a Home Assistant config-flow sign-in experience that lets users pair their own ChatGPT/Codex account using OpenAI Codex device auth, without pasting tokens or reusing Hermes/Codex CLI credentials.

**Architecture:** Home Assistant starts the Codex device-code flow, shows the user a pairing URL and code, polls only when the user clicks Continue, exchanges the returned authorization code for access/refresh tokens, then stores the integration's own tokens in the HA config entry. Runtime requests refresh tokens as needed before calling the Codex backend.

**Reference:** Hermes implementation in `/home/laptop/.hermes/hermes-agent/hermes_cli/auth.py`, `_codex_device_code_login()` lines 6731-6873.

**Tech Stack:** Home Assistant `config_entries.ConfigFlow`, `httpx` via `homeassistant.helpers.httpx_client.get_async_client`, pytest/pytest-asyncio, ruff.

---

## Recommended UX

Use a browser-based device-code flow inside the HA integration setup:

1. User adds **Codex Assist** from Settings → Devices & services.
2. First screen explains the integration is experimental/unsupported and uses the user's ChatGPT/Codex account, not OpenAI API billing.
3. User selects basic settings: model, prompt, safety mode defaulting to `talk_only`.
4. HA calls `https://auth.openai.com/api/accounts/deviceauth/usercode`.
5. HA shows:
   - `https://auth.openai.com/codex/device`
   - the `user_code`
   - expiry/polling guidance
   - a Continue button labeled like “I finished pairing”.
6. User logs in externally in their normal browser and enters/approves the code.
7. Continue polls `https://auth.openai.com/api/accounts/deviceauth/token`.
8. If authorization is still pending, HA redisplays the same code screen with a non-fatal message.
9. If successful, HA exchanges `authorization_code` + `code_verifier` at `https://auth.openai.com/oauth/token`.
10. HA creates config entry with its own token pair and safe options.

Avoid embedded browsers, token paste boxes, copied Hermes tokens, and public callback endpoints.

---

## Exact Codex OAuth Shape from Hermes

### Step 1: request code

POST JSON:

```text
https://auth.openai.com/api/accounts/deviceauth/usercode
```

Body:

```json
{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann"}
```

Important response fields:

```json
{
  "user_code": "...",
  "device_auth_id": "...",
  "interval": 5
}
```

### Step 2: show user URL/code

URL:

```text
https://auth.openai.com/codex/device
```

The user enters `user_code` there.

### Step 3: poll after user clicks Continue

POST JSON:

```text
https://auth.openai.com/api/accounts/deviceauth/token
```

Body:

```json
{"device_auth_id":"...","user_code":"..."}
```

Hermes treats:

- `200` as success with `authorization_code` and `code_verifier`
- `403`/`404` as authorization still pending
- anything else as an error

### Step 4: exchange authorization code

POST form:

```text
https://auth.openai.com/oauth/token
```

Body:

```text
grant_type=authorization_code
code=<authorization_code>
redirect_uri=https://auth.openai.com/deviceauth/callback
client_id=app_EMoamEEZ73f0CkXaXp7hrann
code_verifier=<code_verifier>
```

Expected tokens:

```json
{
  "access_token": "...",
  "refresh_token": "..."
}
```

---

## Implementation Tasks

### Task 1: Extract Codex device-auth helpers

**Objective:** Move device-code request/poll/exchange into testable async helpers.

**Files:**
- Modify: `custom_components/codex_assist/codex_auth.py`
- Test: `tests/test_codex_device_auth.py`

**Steps:**
1. Add failing tests for:
   - request posts JSON to `/deviceauth/usercode`
   - request validates `user_code` and `device_auth_id`
   - poll treats `403`/`404` as pending
   - poll returns `authorization_code` + `code_verifier` on `200`
   - exchange posts form data to `/oauth/token`
2. Implement dataclasses:
   - `CodexDeviceCode`
   - `CodexAuthorizationCode`
   - extend/keep `CodexTokenSet`
3. Implement async methods:
   - `request_device_code()`
   - `poll_device_code()`
   - `exchange_authorization_code()`
4. Run targeted tests.
5. Commit.

### Task 2: Add config-flow state machine

**Objective:** Replace placeholder setup with the HA pairing flow.

**Files:**
- Modify: `custom_components/codex_assist/config_flow.py`
- Test: `tests/test_config_flow_device_auth.py`

**Flow:**
- `async_step_user`: collect model/prompt/safety, start auth
- `async_step_device`: show pairing URL + code
- `async_step_device_wait`: poll once after Continue
- success: exchange token and create entry
- pending: redisplay code screen
- expired/error: show retry/restart path

**Important:** Store `device_auth_id`, `user_code`, `interval`, and start time only in flow instance memory, not config entry data.

### Task 3: Store tokens and refresh safely

**Objective:** Runtime should refresh access tokens before Codex calls and update HA config entry data.

**Files:**
- Modify: `custom_components/codex_assist/codex_auth.py`
- Modify: `custom_components/codex_assist/conversation.py`
- Test: `tests/test_codex_auth.py`, `tests/test_conversation_tokens.py`

**Rules:**
- Integration owns its tokens.
- Do not read or import `~/.hermes/auth.json` or `~/.codex/auth.json`.
- Refresh on expiry/401-like errors.
- Preserve rotated refresh tokens if returned.
- Treat 401/403 refresh failures as reauth required.
- Treat 429 as quota/rate-limited, not relogin-required.

### Task 4: Reauth/options flow

**Objective:** Give users a clean way to fix expired/revoked credentials and change safe settings.

**Files:**
- Modify: `custom_components/codex_assist/config_flow.py`
- Possibly create: `custom_components/codex_assist/const.py`
- Test: `tests/test_reauth_flow.py`, `tests/test_options_flow.py`

**Options:**
- model
- prompt
- safety mode: `talk_only` initially
- future: `exposed_entities_only`, `scripts_only`
- blocked domains default list when tools land later

### Task 5: Documentation pass

**Objective:** Make the README HACS/user ready enough for private testing.

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md` if new guardrails are discovered

**Must document:**
- experimental/unsupported Codex backend
- not affiliated with OpenAI or Home Assistant
- subscription usage/limits may still apply
- integration stores its own refresh token
- do not share tokens with Hermes/Codex CLI/VS Code
- safe default is text-only
- uninstall/revoke guidance

---

## Acceptance Criteria

- User never pastes access/refresh tokens.
- User pairs in external browser via `https://auth.openai.com/codex/device`.
- Integration owns token storage; no Hermes or Codex CLI token import.
- Pending auth is non-fatal and redisplays the code.
- Reauth path exists for invalid/revoked/rotated credentials.
- Text-only conversation works before any HA control tools exist.
- `uv run pytest -q` passes.
- `uv run ruff check .` passes.
