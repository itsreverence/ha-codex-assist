# Architecture

Codex Assist is a Home Assistant custom integration backed by Codex / ChatGPT access. It registers a native Assist conversation agent and a native AI Task provider.

```mermaid
flowchart LR
    Assist[Home Assistant Assist] --> Conversation[Codex Assist conversation agents]
    AITask[Home Assistant AI Task] --> Provider[Codex Assist AI Task provider]

    Conversation --> Runtime[Runtime and token coordinator]
    Provider --> Runtime
    Runtime --> Codex[Codex-compatible backend]

    Conversation --> Bridge[Home Assistant Assist LLM API]
    Bridge --> Exposed[Entities exposed to Assist]

    Codex --> Reply[Streamed conversation reply]
    Codex --> Search[Optional hosted web search]
    Search --> Citations[Validated visual citations]
    Codex --> TaskResult[Text, structured data, or generated image]

    Reply --> Assist
    Citations --> Assist
    TaskResult --> AITask
```

## Main components

- **Config flow** handles Codex-style device-code sign-in. The config entry stores only the OAuth tokens. Reauthentication and reconfiguration replace those tokens and leave agent settings untouched.
- **Config subentries** hold agent settings. Each `conversation` subentry is one Assist conversation agent with its own name, model, prompt, reasoning effort, response length, and web-search toggle. An `ai_task_data` subentry holds the AI Task model, web-search, reasoning, and image-generation settings. `SUBENTRY_SETTING_KEYS` in `config_flow.py` defines which keys each type owns, and the setup flow, subentry flows, and migration all filter through it. Each subentry gets its own service device named after the subentry, and its entity's unique ID is the subentry ID.
- **Subentry reloads** come from an update listener that compares subentry types, titles, and data with a snapshot taken at setup. Adding, editing, or removing an agent reloads the entry. Token rotation also writes the config entry, so changes to entry data alone never reload it.
- **Version 1 migration** moves settings from entry data and options into one `conversation` subentry and one `ai_task_data` subentry, re-points the existing conversation and AI Task registry entries to them without changing their entity IDs, and leaves only the tokens in entry data.
- **Model discovery** uses only visible model IDs returned for the signed-in account. An entry-scoped, in-memory cache survives temporary failures; a small, explicitly unverified fallback list is used only before a successful result.
- **Conversation agents** register one conversation entity per `conversation` subentry, such as `conversation.codex_assist`, for Home Assistant Assist pipelines. Each agent reads its subentry settings at request time.
- **AI Task entity** registers a native provider per `ai_task_data` subentry for text, structured data, supported image attachments, and image generation. It uses fixed AI Task instructions, not a conversation agent's prompt.
- **Runtime token coordinator** serializes refresh-token rotation per config entry. Concurrent Conversation and AI Task requests reuse the winning refresh instead of invalidating one another.
- **Codex client** sends requests to the Codex-compatible service interface and normalizes its response stream.
- **Native transcript state** retains completed provider output items for stateless replay. The state is deep-copy isolated and remains opaque to normal Home Assistant logs, listeners, and conversation traces, which receive only redacted metadata.
- **Hosted web search** is an explicit option. When enabled, it adds the backend `web_search` tool and converts structured URL annotations into a validated source card. Unsupported or unsafe citation URLs are discarded.
- **Assist tool bridge** maps model-requested device actions into Home Assistant's Assist LLM API. It does not call services directly.

## Assist conversation flow

1. Home Assistant sends a voice or chat request through an Assist pipeline using one of the Codex Assist conversation entities.
2. Codex Assist refreshes its stored Codex or ChatGPT token if needed.
3. Codex Assist sends the conversation to the Codex-compatible backend.
4. If Codex requests a Home Assistant tool call, Codex Assist maps that request into Home Assistant's Assist LLM API.
5. Home Assistant validates and executes the tool call using its normal exposed-entity controls.
6. When hosted search is enabled, Codex Assist keeps validated citations in a displayed card and instructs the model to keep raw URLs and source blocks out of spoken prose.
7. Codex Assist returns the final response to Home Assistant.

For stateless multi-turn requests, Codex Assist keeps completed provider output items in Home Assistant's in-memory chat log and replays them before later user or function-output items. This can include encrypted reasoning state and assistant message phase. The integration does not decrypt that state. Native state is removed from normal delta listeners, uses redacted debug formatting, and serializes as an item count rather than provider content in conversation traces.

## AI Task flow

1. Home Assistant sends an AI Task request to the Codex Assist AI Task entity.
2. For data-generation tasks, Codex Assist translates the instructions and supported image attachments into Codex-compatible input items.
3. If Home Assistant supplies a structure, Codex Assist sends it as a native JSON Schema response format and validates the returned data against that structure. Hosted web search stays disabled for this path so citations cannot invalidate the result.
4. For image-generation tasks, Codex Assist requests an image using the configured quality and size.
5. Codex Assist returns text, structured data, or generated image bytes through Home Assistant's native AI Task result types.

Normal Assist conversation surfaces may not expose an upload button even though Home Assistant chat-log objects can carry attachments internally. Use AI Task surfaces that advertise attachment support when testing native attachments.

## Model discovery lifecycle

First setup completes device-code sign-in before model selection. Authenticated
discovery is refreshed when an agent's settings open and every six hours while the entry is
loaded, independently of Assist requests. The periodic callback is cancelled on
unload. Agent settings and periodic discovery resolve and refresh credentials through the
same token coordinator as conversation and AI Task. An HTTP 401 triggers at most
one coordinated refresh and retry.

Each entry caches only model IDs in memory. Results are immutable, concurrent
refreshes are coalesced, successful requests are debounced for 30 seconds, and
failed requests have a 60-second retry backoff. Reauthentication and reconfiguration
clear the cache and invalidate any in-flight result from the previous sign-in.
Successful empty lists remain empty; malformed responses, HTTP errors, and network
failures are distinguished from successful discovery. Cached results and fallback
suggestions are labeled in the form.

The first discovered model in the account’s priority order is preselected for new
setups. Existing models are never changed by discovery:
an absent saved model remains as an explicitly labeled saved choice. The runtime
does not switch models or replay device-control requests after a model rejection.
Image-generation model choices remain a separate curated set. See
[MODEL_DISCOVERY.md](MODEL_DISCOVERY.md) for user-facing behavior.

## Schema conversion compatibility

Assist tool parameters and structured AI Task output use the converter exposed
by Home Assistant's LLM helper. Home Assistant 2026.9 uses Probatio's
`to_openapi`; older supported versions use `convert` from voluptuous-openapi.
The converter must match the helper's serializer and unsupported-value sentinel,
even when both libraries are installed. Home Assistant supplies the matching
library; Codex Assist does not install a separate schema converter.

## Security boundary

Codex or ChatGPT may suggest an action, but Home Assistant remains the execution boundary. Device control goes through Home Assistant's Assist LLM API and is limited to entities exposed to Assist.

Prompts, conversation context, supported AI Task attachments, and hosted-search requests leave the Home Assistant instance when the corresponding feature is used. See [../SECURITY.md](../SECURITY.md) for the full data and control boundaries.

## Intentional non-goals

Codex Assist should not:

- add a custom raw Home Assistant service-call bridge;
- bypass Home Assistant's Assist exposure model;
- require users to expose every entity in their Home Assistant instance;
- add a separate attachment-upload service;
- run a separate always-on local Codex server;
- store screenshots, device codes, access tokens, refresh tokens, cookies, or private Home Assistant URLs in the repository.

## Upstream compatibility

Codex Assist follows the authentication approach used by the official OpenAI Codex CLI. The downstream Codex service interface is not presented as a stable public API contract for third-party Home Assistant integrations. Compatibility may change with upstream Codex updates.
