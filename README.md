# Home Assistant Codex Assist

A custom Home Assistant Assist conversation agent backed by OpenAI Codex / ChatGPT OAuth.

> Experimental: this project is not affiliated with OpenAI or Home Assistant. Codex/ChatGPT OAuth backend access is not an official public OpenAI API surface and may change without notice.

## What it does

Codex Assist registers a native Home Assistant conversation agent so it can be selected in Assist pipeline settings, similar to other LLM conversation providers, while using Codex OAuth access instead of a normal OpenAI API key.

It is designed to behave like a normal Home Assistant voice-assistant provider:

- installed as a custom integration under `custom_components/codex_assist`
- configured through Home Assistant config and options flows
- authenticated with its own device-code OAuth tokens owned by Home Assistant
- registered as `conversation.codex_assist`
- connected to Home Assistant's standard Assist LLM API tools for exposed-entity control

## Current status

Working local MVP, smoke-tested on Home Assistant `2026.5.4`:

- Codex device-code sign-in flow
- runtime access-token refresh before model calls
- streaming Codex Responses request support
- Home Assistant conversation entity registration
- Home Assistant chat-log history support for follow-up turns
- native Home Assistant Assist control through the built-in LLM API and exposed-entity surface
- live smoke test: listed exposed lights and turned exposed lights on/off through Assist tools

Still planned before a wider release:

- broader safe-command smoke tests across read-only devices, fans, media, and climates
- release packaging and public issue templates

## Control and safety stance

Codex Assist intentionally follows Home Assistant's normal LLM voice-provider path instead of inventing an arbitrary service-call bridge:

1. registers as a native `ConversationEntity`
2. sets `ConversationEntityFeature.CONTROL`
3. calls `chat_log.async_provide_llm_data(..., llm.LLM_API_ASSIST, ...)`
4. formats HA LLM tools as Codex Responses function tools
5. executes model-requested tool calls through `chat_log.async_add_assistant_content(...)`
6. returns the final response with `conversation.async_get_result_from_chat_log(...)`

That means control is bounded by Home Assistant's normal Assist LLM API and the entities you expose to Assist. The integration does **not** provide a custom raw “call any service” escape hatch.

Before using it for day-to-day control:

- review Home Assistant's exposed-entity settings
- start with harmless lights or read-only questions
- keep locks, alarms, water shutoff valves, garage doors, covers, and other sensitive devices unexposed unless you deliberately want Assist control there

## Installation for development / custom repository use

Until this is published publicly, install it manually or as a private/custom HACS repository.

Manual install:

1. Copy `custom_components/codex_assist` into your Home Assistant config directory:

   ```text
   /config/custom_components/codex_assist
   ```

2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**.
4. Search for **Codex Assist**.
5. Follow the device-code login instructions.
6. Select `Codex Assist` in an Assist pipeline and test with a harmless command.

HACS custom repository install:

1. Add this repository as a HACS custom repository.
2. Category: **Integration**.
3. Install **Codex Assist**.
4. Restart Home Assistant.
5. Add/configure the integration from **Devices & services**.

## Troubleshooting

### `conversation.codex_assist` does not show up

1. Confirm the integration is configured under **Settings → Devices & services**.
2. Restart Home Assistant after copying files or installing with HACS.
3. Reload the config entry if the integration exists but the entity is missing.
4. Check Home Assistant logs for `codex_assist` import/setup errors.

### The agent answers but cannot control anything

1. Confirm the entity state has `supported_features: 1`.
2. Confirm your Assist pipeline is using `Codex Assist`.
3. Review Home Assistant's exposed entities for Assist/conversation.
4. Test with a harmless exposed light first.

### Token/auth failures

Token refresh runs before each model call. If refresh fails, Codex Assist starts a Home Assistant reauth flow and tells you to sign in again from Home Assistant repairs or the integration page.

## Development checks

```bash
uv run pytest -q
uv run ruff check .
```

CI runs the same test and lint gates on pushes and pull requests.

## Public release positioning

The practical public path is:

1. publish a public GitHub repository
2. support HACS custom repository installs first
3. add releases and basic support docs
4. only then consider HACS default inclusion or upstream Home Assistant Core discussions

Home Assistant Core inclusion is unlikely while the integration depends on unsupported/undocumented Codex OAuth backend endpoints. A realistic Core path would require a stable, documented OpenAI-supported API surface, a maintained Python client or clean internal client, tests, docs, code owners, config/reauth quality, and a clear long-term maintenance commitment.
