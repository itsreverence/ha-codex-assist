# Codex hosted web-search contract spike

Status: **released in v0.4.0 with fixture, real-Home-Assistant contract, and manual Home Assistant acceptance coverage. The standalone sanitized probe still requires a Codex Assist-owned OAuth grant.**

Foundation checkpoint: `d8e2197` (`Harden Codex runtime contracts`)

## Question

Can Codex Assist add hosted web search through its existing Responses request seam without weakening the Home Assistant tool boundary or losing citations?

## Evidence

### First-party OpenAI Responses contract

At OpenAI Python commit [`9917c6e`](https://github.com/openai/openai-python/tree/9917c6e28e66e90e1227b3d223c06a8c5441515a/src/openai/types/responses):

- [`web_search_tool_param.py`](https://github.com/openai/openai-python/blob/9917c6e28e66e90e1227b3d223c06a8c5441515a/src/openai/types/responses/web_search_tool_param.py) defines hosted tools with `type: "web_search"` (or the dated variant). It supports optional domain filters, `low|medium|high` search context, approximate location, and an external-web-access switch.
- The [OpenAI web-search guide](https://platform.openai.com/docs/guides/tools-web-search) documents `include: ["web_search_call.action.sources"]` when callers need the complete consulted-source list. It also documents model-specific limitations, including that GPT-5 web search is incompatible with minimal reasoning.
- Search execution emits distinct progress events:
  - [`response.web_search_call.in_progress`](https://github.com/openai/openai-python/blob/9917c6e28e66e90e1227b3d223c06a8c5441515a/src/openai/types/responses/response_web_search_call_in_progress_event.py)
  - [`response.web_search_call.searching`](https://github.com/openai/openai-python/blob/9917c6e28e66e90e1227b3d223c06a8c5441515a/src/openai/types/responses/response_web_search_call_searching_event.py)
  - [`response.web_search_call.completed`](https://github.com/openai/openai-python/blob/9917c6e28e66e90e1227b3d223c06a8c5441515a/src/openai/types/responses/response_web_search_call_completed_event.py)
- [`response_function_web_search.py`](https://github.com/openai/openai-python/blob/9917c6e28e66e90e1227b3d223c06a8c5441515a/src/openai/types/responses/response_function_web_search.py) defines `web_search_call` output items with search, open-page, or find-in-page actions and a status.
- Citations are structured output annotations, not merely prose conventions. [`response_output_text_annotation_added_event.py`](https://github.com/openai/openai-python/blob/9917c6e28e66e90e1227b3d223c06a8c5441515a/src/openai/types/responses/response_output_text_annotation_added_event.py) defines `response.output_text.annotation.added`; `url_citation` includes title, URL, and text-span indexes. [`response_output_text.py`](https://github.com/openai/openai-python/blob/9917c6e28e66e90e1227b3d223c06a8c5441515a/src/openai/types/responses/response_output_text.py) also carries annotations on completed output text.

These files are generated from the first-party [OpenAI OpenAPI repository](https://github.com/openai/openai-openapi/tree/172101000e7be21103c405aa8bedf918039f886f).

This is strong evidence for the public Responses contract, but it does **not** prove that the private ChatGPT endpoint used here—`https://chatgpt.com/backend-api/codex/responses`—accepts every field or emits every documented event. That gap is exactly what the live probe must close.

### Public fork evidence

Fork commit [`d73bed5`](https://github.com/greimela/ha-codex-assist/commit/d73bed5b5426) adds a default-off option and appends exactly `{"type": "web_search"}` to the same tools list as HA function tools. That is a plausible request shape and matches the first-party contract.

The follow-up commits [`9876bb8`](https://github.com/greimela/ha-codex-assist/commit/9876bb807ee7) and [`a0b12a7`](https://github.com/greimela/ha-codex-assist/commit/a0b12a7e902b) strip Markdown links/source sections with regular expressions for TTS. They do **not** parse or preserve `url_citation` annotations, record raw web-search events, prove model/plan support, or show hosted CI/live probe evidence. Therefore the fork demonstrates a small product wiring idea, not a verified backend contract.

## Repository fit

The existing `CodexClient` already passes tool dictionaries through the Responses payload, so no provider abstraction or plugin registry is needed. However, its public stream delta model currently represents only text and HA function calls. Unknown search progress and annotation events are ignored.

That means simply copying the fork could produce text, but it would throw away the structured citation contract and then guess at citations with regex. We should not ship that behavior.

## Privacy-safe probe artifact

`scripts/probe_web_search_contract.py` sends one fixed request:

- no HA state, entity IDs, conversation history, attachments, tool results, location, or user text;
- `store: false`;
- `search_context_size: low`;
- explicit external-web access and `include: ["web_search_call.action.sources"]`;
- results restricted to `iana.org`;
- sanitized output containing event names/key shapes and annotation types only—never response text, queries, URLs, IDs, or credentials.

Dry run:

```bash
uv run python scripts/probe_web_search_contract.py --dry-run
```

Live run requires an access token issued to **Codex Assist itself**:

```bash
CODEX_ASSIST_ACCESS_TOKEN='[ephemeral integration-owned token]' \
  uv run python scripts/probe_web_search_contract.py
```

Do not source that token from Codex CLI, an editor, or another assistant. The probe intentionally refuses to run without an explicitly supplied integration-owned token.

## Executed result

- Dry run succeeded and produced the fixed request payload.
- Privacy/sanitization tests passed.
- No integration-owned token exists in the repository checkout, so the standalone live probe was **not** run.
- No Codex CLI/editor credentials were inspected or copied.
- The released Home Assistant path completed current-information search, rendered structured citations, and completed short and long spoken-response acceptance checks without a new integration error.

## Decision

**Keep web search off by default and revalidate the observed contract when its request shape, supported models, or citation handling changes.** The released implementation adds the tool at the existing request seam and coexists with Home Assistant tools in fixture, real-Home-Assistant contract, and manual acceptance tests.

The observed Home Assistant path now:

1. accepts `web_search` on the tested model and account;
2. preserves structured URL citations from the response stream;
3. validates citation URLs before displaying them in a separate card;
4. instructs the model to keep raw URLs and generated source blocks out of spoken output;
5. keeps search disabled for schema-constrained AI Tasks.

The standalone sanitized probe remains useful when the backend contract changes. It requires a dedicated Codex Assist OAuth authorization and should establish:

1. the exact progress, output, and annotation events emitted by the current backend;
2. unsupported-model and usage-limit error shapes;
3. whether request fields still match the tested contract;
4. whether citation display works and the model instruction to omit citations is honored in tested speech.

Keep search opt-in. Never automatically turn Home Assistant state, attachments, location, or tool output into search queries.

Do not hard-code a presumed HTTP status, error code, or retry rule for unsupported models. Classify capability, authentication, quota, and transient failures only from an observed HTTP or SSE payload. Retain the integration's bounded retry policy for genuinely transient failures.
