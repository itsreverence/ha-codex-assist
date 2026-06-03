# Architecture

Codex Assist is a Home Assistant custom integration that registers a native Assist conversation agent backed by Codex / ChatGPT access.

![Codex Assist architecture](../assets/codex-assist-architecture.png)

## Main components

- **Config flow**: handles Codex-style device-code sign-in, stores OAuth tokens in the Home Assistant config entry, and exposes options such as the selected Codex model.
- **Model discovery**: offers a curated fallback model list and, when authenticated, asks the Codex backend for the currently available model IDs.
- **Conversation agent**: registers `conversation.codex_assist` so Codex Assist can be selected in Home Assistant Assist pipelines.
- **Codex client**: refreshes tokens when possible and sends conversation turns to the Codex-compatible service interface.
- **Assist tool bridge**: maps model-requested actions into Home Assistant's Assist LLM API rather than calling services directly.

## Request flow

1. Home Assistant sends a voice/chat request through an Assist pipeline using `conversation.codex_assist`.
2. Codex Assist refreshes its stored Codex/ChatGPT token if needed.
3. Codex Assist sends the conversation to the Codex-compatible service interface.
4. If Codex requests a Home Assistant tool call, Codex Assist maps that request into Home Assistant's Assist LLM API.
5. Home Assistant validates and executes the allowed Assist tool call using its normal exposed-entity controls.
6. Codex Assist returns the final response to Home Assistant.

## Security boundary

Codex / ChatGPT may suggest or request an action, but Home Assistant remains the execution boundary. Device control is routed through Home Assistant's Assist LLM API and limited to entities exposed to Assist.

For the full security stance and exposed-entity guidance, see [../SECURITY.md](../SECURITY.md).

## Intentional non-goals

Codex Assist should not:

- add a custom raw Home Assistant service-call bridge;
- bypass Home Assistant's Assist exposure model;
- require users to expose every entity in their Home Assistant instance;
- run a separate always-on local Codex server;
- store screenshots, device codes, access tokens, refresh tokens, cookies, or private Home Assistant URLs in the repository.

## Upstream compatibility

Codex Assist follows the authentication approach used by the official OpenAI Codex CLI. The downstream Codex service interface is not currently presented as a stable public API contract for third-party Home Assistant integrations, so compatibility may change with upstream Codex updates.
