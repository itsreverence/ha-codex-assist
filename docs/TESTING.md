# Testing

## Automated checks

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run pytest -q
```

The fast suite under `tests/` uses lightweight Home Assistant fakes. Run the `tests_ha/` suite in an isolated Python 3.14 environment so it does not reuse the normal project environment:

```bash
uv venv --python 3.14 .venv-ha
uv pip install --python .venv-ha/bin/python -r requirements_test_ha.txt \
  --override requirements_test_ha_overrides.txt
.venv-ha/bin/python -m pytest tests_ha -q

uv run --isolated --python 3.14 --with-requirements requirements_test_ha_min.txt \
  python -m pytest tests_ha -q
```

`tests_ha/test_single_entry.py` verifies duplicate setup stops before authentication
and existing entries can complete reauthentication and reconfiguration. It checks
the reload request; the reload implementation is stubbed in these flow tests.

### Home Assistant version selection

CI tests compatibility boundaries rather than every monthly or patch release:

- **2026.6.0** is the minimum supported version.
- **2026.8.3** exercises the legacy converter immediately before the Probatio transition.
- **2026.9.0** exercises Probatio with the old tool-result API.
- **2026.10.0** exercises the current stable release with `ToolResult` and no converter exports.

These checks do not establish that every omitted release, including 2026.7,
has been tested. Add a version when a relevant API change or reported regression
makes it a distinct compatibility case. Keep the minimum and latest stable
targets, and retain an intermediate target while it covers a separate supported
API contract. Review the matrix with each monthly HA release and update the
stable pins together. All lanes also run weekly, but pinned weekly runs do not
discover new HA releases automatically.

The current test plugin, `pytest-homeassistant-custom-component==0.13.370`,
pins HA `2026.10.0b4`. The test-only override installs `2026.10.0` instead.
Remove that override when an aligned plugin version is available and verified;
do not silently test a beta while claiming stable coverage.

### Test ownership

Before adding a test, identify its observable contract, a credible regression,
and why existing coverage cannot catch it. Prefer extending a case at the
owning boundary over duplicating it at several layers. Do not add production
exports or wrappers solely for tests. Demonstrate regression tests failing on
the old code for the intended reason before fixing it.

The fast suite covers local behavior with lightweight fakes. The HA contract
suite exercises real framework types and lifecycle calls with external backend
responses stubbed. It does not establish live authentication or device behavior.

`tests/test_codex_stream_completion.py` checks the shared response completion
contract through an HTTPX byte stream. It covers EOF and `[DONE]` without
completion, text delivery before completion, held tool calls, image previews,
explicit backend failures, and stopping at completion without waiting for EOF.
Successful SSE fixtures must include `response.completed` explicitly; generic
fake transports must not add it automatically.

`tests_ha/test_stream_completion.py` checks incomplete conversation and AI Task
delivery, rejection of incomplete images without persisted media, native tool
execution only after completion, and auth retry only before visible output.
The existing search delivery cases check that hidden search rounds still allow
the single auth-refresh retry. Run these with both HA contract environments.

`tests/test_codex_text_recovery.py` exercises completed-message recovery through
synthetic HTTPX SSE streams. It checks exact suffixes, whitespace, item aliases,
multiple text parts, repeat completions, and explicit failure when recovery
would be ambiguous or reorder delivered text. The real-HA completion and search
delivery cases also check completed-only answers, silent terminal responses,
auth failures after recovered text, and no Sources card on success or failure.
An auth retry must discard native state from the failed attempt. All backend
responses in these tests are synthetic; passing them does not establish live
Codex behavior.

`tests_ha/test_conversation_errors.py` calls HA's public conversation API with
synthetic HTTPX responses and registered native test tools. It checks fixed
speech for HTTP, SSE, transport, quota, authentication, and tool exceptions;
safe replay history and conversation-owned logs; refresh and reauthentication
counts; cancellation propagation; and no action replay after failed synthesis.
Chained exceptions carry synthetic sensitive messages and a private traceback path.
Diagnostics distinguish backend failure, empty-answer, and incomplete-stream sites
by integration filename and line number, without source text or absolute paths.
Authentication failures without reauthentication use generic speech. Reauthentication
keeps its short guidance, and quota speech makes no temporary-recovery promise.
No conversation result may attach a Sources card. Existing search handoff coverage
also checks this after an earlier search round. The request assertions verify
that conversation error guidance reaches the backend with custom prompts and
either search setting.

These tests run in both HA contract environments. They do not prove model
obedience, filter tool-result payloads, or establish framework-wide log or trace
secrecy. AI Task assertions retain their existing error expectations. No live
backend or Home Assistant device is involved.

The tool-result regression runs a real conversation and checks the outgoing
Codex payload. On modern HA it removes only the deprecated property to prove
the integration does not rely on that compatibility shim.

`tests_ha/test_ai_task_delivery.py` owns AI Task delivery. It calls HA's public
`async_generate_data` API and `ai_task.generate_data` / `ai_task.generate_image`
services with only external Codex HTTP replaced by a synthetic SSE transport.
HA supplies the real task objects, chat logs, service selector schemas, results,
attachment resolution, image persistence, and signed media representation.
The cases cover plain text, structured data (including optional null omission
and disabling hosted search), a local PNG attachment reaching backend HTTP,
and image bytes plus configured metadata reaching HA's media store. Invalid-image
cases cover both services with an actual 10 MiB overflow and a targeted local-file
open failure. They require a service error, zero backend requests, no generated
media, and no synthetic private-path marker in delivered errors, tracebacks, or
Codex Assist logs. Home Assistant core can log attachment metadata at debug level;
these tests do not claim framework-wide log redaction. They do
not establish live authentication, backend image generation, camera contents,
or device behavior. The synthetic PNG is not evidence that a backend honored
the requested image dimensions.

These delivery cases also enforce all three feature flags through HA's
admission checks, replacing the fast test's numeric flag assertion. Its
entry-scoped identity assertions remain. Schema edge cases and auth/retry
coverage remain separate; this is not a broad test-pruning pass.

#### AI Task mutation evidence

The following temporary source mutations were actually run against HA
2026.10.0, one focused test at a time. Each produced **1 failed**, exit code 1,
for the indicated contract. Both production files were restored byte-for-byte
and checked with `git diff --exit-code` before the unmutated validation runs.
No mutation or test-only production seam is retained.

The test names below are in `tests_ha/test_ai_task_delivery.py`; each probe used
`python -m pytest -c pyproject.toml <file>::<test_name> -q --tb=short` in the
current HA environment described above.

| Temporary mutation | Test | Observed failure |
| --- | --- | --- |
| Remove `GENERATE_DATA` from the entity flags | `test_generate_data_returns_native_plain_text_result` | HA rejects generating data |
| Remove `SUPPORT_ATTACHMENTS` from the entity flags | `test_generate_data_service_sends_local_image_attachment` | HA rejects attachments |
| Remove `GENERATE_IMAGE` from the entity flags | `test_generate_image_service_persists_native_result_as_media` | HA rejects generating images |
| Return empty `GenDataTaskResult.data` | `test_generate_data_returns_native_plain_text_result` | Delivered text differs |
| Set the data path's `text_format` to `None` | `test_generate_data_service_delivers_selector_validated_structure` | Backend request lacks `format` |
| Pass `None` instead of user attachments to conversion | `test_generate_data_service_sends_local_image_attachment` | Backend receives string content instead of multimodal content |
| Return `dict` instead of native `GenImageTaskResult` | `test_generate_image_service_persists_native_result_as_media` | HA cannot call `as_dict` |
| Set native image result's `image_data` to empty bytes | `test_generate_image_service_persists_native_result_as_media` | Persisted bytes differ |

The older lanes also run with `probatio==0.11.2` installed alongside
`voluptuous-openapi`. This catches converter/serializer mismatches when both
packages are available. Codex Assist selects the converter used by Home
Assistant's LLM helper, rather than inferring it from installed packages.

```bash
uv run --isolated --python 3.14 --with-requirements requirements_test_ha_202609.txt \
  python -m pytest tests_ha -q

uv run --isolated --python 3.14 --with-requirements requirements_test_ha_previous.txt \
  python -m pytest tests_ha -q

uv run --isolated --python 3.14 --with-requirements requirements_test_ha_min.txt \
  --with probatio==0.11.2 python -m pytest tests_ha -q

uv run --isolated --python 3.14 --with-requirements requirements_test_ha_previous.txt \
  --with probatio==0.11.2 python -m pytest tests_ha -q
```

## Hosted-search compatibility

`tests/test_codex_client_web_search.py` covers the standalone search loop through
synthetic HTTP and SSE responses. It checks intermediate-text suppression,
search-call budgets, cancellation, auth and rate-limit errors, and streaming on
paths that cannot start another internal search round.

`tests_ha/test_web_search_delivery.py` exercises the real conversation and AI Task
owners with backend HTTP substituted. It checks search followed by URL opening,
final-only speech and task data, absent source cards, next-turn native replay,
HA tool handoff, and search-endpoint 401 refresh. The handoff and next-user-turn
checks require a unique searched fact and URL in the outgoing model payload,
without repeating either in the final answer. They also check complete search
call/output pairs, preserved reasoning and raw provider annotations, a single
HA execution, and private-state
redaction in HA logs, subscriptions, and traces. These tests do not establish
live endpoint acceptance or account/model compatibility.

Before releasing a search-path change, test the exact candidate against the real
backend with integration-owned authentication: a search, a direct URL open, a
follow-up question, and a search followed by a harmless HA tool. Confirm that
speech and task data contain only the final answer, and inspect retained history
for backend rejection on the follow-up. Do not borrow another application's
credentials. Keep tokens and raw native state out of test reports.

Replace `MODEL_ID` below with a model from the integration’s account-discovered
list. The probe requires an explicit model so it cannot silently test a retired
hardcoded default.

To exercise the integration's client-side search path, run:

```bash
uv run python scripts/probe_client_search.py --model MODEL_ID --dry-run
```

With separate live authorization and `CODEX_ASSIST_ACCESS_TOKEN` already set to
an integration-owned token, omit `--dry-run`. This probe performs a direct query,
a direct URL open, a model-selected search, and native replay. Its output contains
sanitized counts and booleans, not answers, URLs, tokens, or raw native state.
The probe requires every direct and model-selected search to return HTTP 200
with usable, nonempty normalized output. Errors, malformed output, and empty
results fail the gate even if the model still answers; direct-stage successes
cannot substitute for model-selected search success. Completion proves exercised
transport paths, not semantic answer accuracy.
The probe does not connect to HA or verify device effects, speech, or UI behavior.
Test those boundaries separately through the real owners.

When the hosted-search payload, model defaults, search delivery, or backend contract changes:

1. Run `uv run python scripts/probe_web_search_contract.py --model MODEL_ID --dry-run` and its tests.
   The probe checks the backend's built-in `web_search` tool. The integration does not use that
   tool (it runs search on `alpha/search`, see [ARCHITECTURE.md](ARCHITECTURE.md)), so the probe
   shows whether the built-in tool works again, not whether integration search works.
2. In Home Assistant, enable web search and ask a current-information question that requires search.
3. Check the spoken answer for raw URLs, markdown links, or a source block. Confirm
   the response has no separate Sources card. Custom API consumers no longer receive
   Sources metadata in `response.card`. Test speech and target-client behavior
   separately from backend response data.
4. Verify a long spoken answer completes without a new Codex Assist or audio error.
5. If an integration-owned OAuth token is available, run the sanitized live probe:

   ```bash
   CODEX_ASSIST_ACCESS_TOKEN='[ephemeral integration-owned token]' \
     uv run python scripts/probe_web_search_contract.py --model MODEL_ID
   ```

   The probe emits closed outcomes and booleans, not event names, key shapes,
   response text, search queries, URLs, identifiers, or credentials. Success
   requires `response.completed`, a completed search item, and a nonempty answer.
   Never borrow credentials from Codex CLI, an editor, or another assistant.

## Release-candidate install

1. Download the branch or tag archive to test.
2. Back up the installed integration.
3. Copy `custom_components/codex_assist` to `/config/custom_components/codex_assist`.
4. Restart Home Assistant.
5. Confirm the integration version and logs reflect the candidate.

To roll back, reinstall the latest stable release through HACS and restart Home Assistant.

## Assist smoke test

After restarting Home Assistant:

1. Confirm `conversation.codex_assist` exists.
2. Select Codex Assist in an Assist pipeline.
3. Ask a read-only question and ask it to list exposed entities.
4. Test one harmless exposed light.
5. Confirm sensitive entities remain unexposed unless deliberately allowed.

## Clarification follow-ups

`tests_ha/test_clarification_followups.py` calls public HA Conversation and AI Task
APIs with scripted HTTPX responses. Outbound-request assertions check that
clarification guidance accompanies default, custom, options, template, and extra
prompts without mutating stored prompts or accumulating across turns. They also
check search guidance, auth retries, native tool handoffs, and AI Task exclusion.

Separate characterization cases check native continuation for final `?`, ASCII
`;` (U+003B), and fullwidth `？`, retained conversation ID and history, and no continuation
after ordinary, action, or cancellation acknowledgements. Ellipses, periods,
trailing prose or quotes, and Arabic `؟` remain unchanged and do not continue.
A completed-message-only question still reaches speech and triggers native
continuation after search, without a separate Sources card. A fixed
clarification-then-`HassTurnOn` script checks zero calls before clarification and
exactly one call to a synthetic light service afterward.
The script never chooses a response based on the new instruction. This proves
request construction and HA routing only, not model compliance or real device
behavior. Run the file and the full suite in both HA contract environments.

Before claiming live clarification or ESPHome support, use an authorized test
instance with integration-owned authentication and complete these checks:

1. Record the candidate commit, HA version, selected model, pipeline language,
   satellite model and firmware, and search setting. Keep private identifiers out
   of shared evidence. Test the default prompt and the installation's custom prompt.
2. Ask an ambiguous read-only question. Check that the actual model asks one direct
   question as the final sentence with nothing after its question punctuation.
   Inspect `continue_conversation` and the conversation ID in the Assist result.
3. On the physical ESPHome satellite, confirm the microphone listens again after
   TTS. Answer without another wake word and confirm the same conversation resumes
   with the earlier question in context. A true result flag alone is not this proof.
4. With only harmless test lights exposed, give an ambiguous action. Confirm no
   action occurs before clarification. Name the target, then confirm exactly one
   intended action and no unnecessary closing question or extra listening cycle.
5. Repeat clarification and answer with "cancel" and "never mind". Confirm no
   action occurs and listening ends. Separately test the satellite stop control
   and the existing timeout without changing microphone or pipeline settings.
6. Check an ordinary answer and a completed action for a false continuation flag
   and normal idle behavior. Enable search and verify a clarification has no URLs
   or source text after the spoken question and no separate Sources card.
7. Test the languages used by the installation. HA's native check recognizes `?`,
   ASCII `;` (U+003B), and fullwidth `？`. Record other punctuation as a known limitation;
   do not repair output or force English to make the test pass.

Report observed failures as well as successes. Do not claim live model compliance,
physical microphone reopening, or timeout and cancellation behavior from the
scripted suite. Publish matching wiki guidance only after the repository docs
reach `main`, as required by [RELEASING.md](RELEASING.md).

## Authentication and model tests

The auth boundary regressions use real `httpx` responses and transport exceptions
for device-code requests, polling, code exchange, and refresh. Coordinator and
discovery tests use the real auth client to check temporary failures, unchanged
credentials, still-valid-token fallback, and authentication versus unavailable
classification.

The stubbed exchange-recovery case does not establish that a live one-time code
can be reused after a lost exchange response. Restart sign-in if pairing cannot
finish after an exchange failure.

`tests_ha/test_auth_recovery.py` drives HA's flow manager through setup, reauth,
and reconfigure failures, then resumes each flow. It also calls public Conversation
and AI Task APIs through temporary refresh failure and recovery, and opens options
with cached models after a failed refresh. External HTTP is stubbed; the options
cache uses an injected clock to advance its cooldown. Run this file in both HA
contract environments. These tests do not establish live sign-in or backend
availability.

When auth or model handling changes:

- verify invalidated credentials produce a clear reauthentication path;
- complete device-code sign-in and confirm the existing config entry resumes;
- confirm logs do not expose tokens, cookies, or device codes;
- verify fallback models appear when discovery is unavailable;
- verify authenticated model discovery when the backend supports it;
- verify an unlisted saved model is labeled and remains selected when other options are saved;
- verify discovery failure offers an explicitly unverified fallback only when no successful cache exists;
- verify a successful empty account list does not become a fallback list;
- verify setup asks for the model only after sign-in;
- verify concurrent refresh, 401 retry, cache invalidation after reauth, and periodic-refresh unload cleanup;
- verify new setup preselects the first advertised model, regardless of its model ID.

## AI Task and media tests

Home Assistant's normal Assist popup may not expose file uploads. Use AI Task surfaces for native attachment testing.

1. Confirm the Codex Assist AI Task entity exists.
2. Call `ai_task.generate_data` with a small local image or camera attachment and verify the response uses its contents.
3. Call `ai_task.generate_image` with a plain prompt and one non-default size.
4. Confirm text-only Assist still works afterward.
5. Confirm logs do not contain tokens, local file contents, or base64 payloads.

Codex Assist accepts up to four image attachments, with a 10 MiB per-image limit and
a 20 MiB aggregate limit per request. For nonblank instructions, missing paths,
unreadable images, and requests
over any attachment limit fail before a model request. OS errors must not expose
attachment paths in delivered errors or Codex Assist logged tracebacks. Non-image MIME types
remain ignored.

Repeat attachment checks through both AI Task services. Confirm a valid PNG reaches
the backend, then confirm an image over 10 MiB fails without a model request.
Fast boundary tests cover stat, open, and read failures, mixed valid and invalid
images, exact limits, and growth during a bounded read.

Before publishing screenshots, remove private URLs, account details, tokens, device codes, and private entity or dashboard names.
