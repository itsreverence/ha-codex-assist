# Home Assistant Codex Assist

A custom Home Assistant Assist conversation agent backed by OpenAI Codex / ChatGPT OAuth.

This project is experimental and not affiliated with OpenAI or Home Assistant.

## Goal

Register a native Home Assistant conversation agent so it appears in Assist pipeline settings, similar to other LLM conversation integrations, while using Codex OAuth access instead of a normal OpenAI API key.

The integration should feel like a normal Home Assistant voice-assistant provider:

- installed as a custom integration under `custom_components/codex_assist`
- configured through Home Assistant config and options flows
- authenticated with its own device-code OAuth tokens owned by Home Assistant
- registered as `conversation.codex_assist` for Assist pipelines
- text-only by default, with Home Assistant control tools added only through explicit curated safety modes

## Current status

Working local MVP:

- Codex device-code sign-in flow
- runtime access-token refresh before model calls
- streaming Codex Responses request support
- Home Assistant conversation entity registration
- Home Assistant chat-log history support for follow-up turns
- safe `talk_only` mode; no broad Home Assistant control tools exposed yet

## Safety and compatibility stance

This is intentionally not a shell bridge and not a Larry-specific one-off. The control path should follow Home Assistant's built-in Assist/LLM API pattern when added:

1. keep `talk_only` as the default
2. expose `ConversationEntityFeature.CONTROL` only when an explicit tool-access option is enabled
3. use Home Assistant's LLM API/tool plumbing rather than direct arbitrary service calls
4. rely on Assist/entity exposure controls for what the model can see or control
5. document that Codex OAuth/backend access is experimental and not an official OpenAI public API surface

## Development checks

```bash
uv run pytest -q
uv run ruff check .
```
