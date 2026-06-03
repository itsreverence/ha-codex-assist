# Codex Assist for Home Assistant

<p align="center">
  <img src="assets/codex-assist-icon.png" alt="Codex Assist icon" width="128" height="128">
</p>

Use OpenAI Codex / ChatGPT as a Home Assistant Assist conversation agent.

Codex Assist installs as a custom Home Assistant integration, signs in with Codex-style ChatGPT device-code auth, and lets Assist answer questions or control only the Home Assistant entities you expose to Assist.

> Experimental: this project is not affiliated with OpenAI or Home Assistant. It follows the authentication approach used by the official OpenAI Codex CLI, but the downstream Codex service interface is not currently presented as a stable public API for third-party Home Assistant integrations. Compatibility may change with upstream Codex updates.

## Install with HACS

1. In Home Assistant, open **HACS**.
2. Open the three-dot menu → **Custom repositories**.
3. Add this repository:

   ```text
   https://github.com/itsreverence/ha-codex-assist
   ```

4. Set category to **Integration**.
5. Install **Codex Assist**.
6. Restart Home Assistant.
7. Go to **Settings → Devices & services → Add integration**.
8. Search for **Codex Assist**.
9. Follow the device-code sign-in flow.
10. Select **Codex Assist** in your Assist pipeline and test with something harmless, like an exposed light.

## What it does

- Adds `conversation.codex_assist` as a native Home Assistant conversation agent.
- Uses Home Assistant config and reauth flows.
- Refreshes Codex/ChatGPT access tokens before model calls.
- Supports follow-up chat context from Home Assistant Assist.
- Uses Home Assistant's built-in Assist LLM API for exposed-entity control.

## Safety model

Codex Assist does **not** expose a raw “call any Home Assistant service” bridge.

Control goes through Home Assistant's normal Assist LLM tool system, so the practical safety boundary is your **Assist exposed entities** list.

Before daily use:

- expose only devices you actually want Assist to control
- start with harmless lights or read-only questions
- keep locks, alarms, garage doors, water shutoff valves, covers, and other sensitive devices unexposed unless you deliberately want voice/LLM control there

## Troubleshooting

### `conversation.codex_assist` does not appear

- Restart Home Assistant after installing or updating.
- Confirm **Codex Assist** is configured under **Settings → Devices & services**.
- Reload the config entry if the integration exists but the entity is missing.
- Check Home Assistant logs for `codex_assist` setup errors.

### The agent answers but cannot control devices

- Confirm your Assist pipeline uses **Codex Assist**.
- Confirm the target device/entity is exposed to Assist.
- Test with a simple exposed light first.

### Auth stops working

Codex Assist refreshes tokens before each model call. If refresh fails, Home Assistant should start a reauth flow from Repairs or the integration page.

## Manual development install

For development only, copy the integration folder into Home Assistant:

```text
/config/custom_components/codex_assist
```

Then restart Home Assistant and add **Codex Assist** from **Settings → Devices & services**.

## Development checks

```bash
uv run ruff check .
uv run pytest
```

## Status

Current release: `v0.1.2`.

Smoke-tested on Home Assistant `2026.5.4` with:

- HACS custom repository install
- Codex device-code sign-in
- access-token refresh
- Assist conversation registration
- exposed-light listing and on/off control through Home Assistant Assist tools

A wider/default-listing release still needs broader smoke testing across read-only devices, fans, media players, climates, and user feedback on upstream Codex compatibility.
