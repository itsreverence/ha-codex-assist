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
final-only speech and task data, safe source cards, next-turn native replay,
HA tool handoff, and search-endpoint 401 refresh. The handoff and next-user-turn
checks require a unique searched fact and URL in the outgoing model payload,
without repeating either in the final answer. They also check complete search
call/output pairs, preserved reasoning, a single HA execution, and private-state
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
The probe does not connect to HA or verify device effects, speech, source cards,
or other UI behavior. Test those boundaries separately through the real owners.

To compare client-side and built-in search with the same public questions, run:

```bash
uv run python scripts/probe_search_comparison.py --model MODEL_ID --dry-run
```

The comparison accepts one required account-discovered `--model`, `--dry-run`,
and `--first-arm client|builtin`, which defaults to `client`. It has no prompt,
token, pair-count, multiple-model, or budget flags. Dry-run reports the fixed
limits with `live_request_sent=false` and makes no requests. For an authorized
live run, use only the integration-owned `CODEX_ASSIST_ACCESS_TOKEN` environment
variable and omit `--dry-run`. Do not borrow another application's credentials.

Each invocation pairs both arms for exactly three fixed public cases. The cases
ask who maintains IANA's Reserved Domains page, the current latest stable Python
release, and a direct open of IANA's example-domains page. The first arm alternates
between cases. Repeat the invocation with the other `--first-arm` value for a
second window, and run each additional model separately with both orders.

Both arms use the same initial prompt, instructions, model options, medium
search context, and enabled external access, without domain filters or location.
The client arm uses `CodexClient.stream_turn` and its real `alpha/search` adapter.
The built-in arm uses the existing `_stream_once` iterator with `web_search`.
Each arm owns a fresh HTTP client. An initial turn must complete normally, answer,
retain native items, and establish successful search before its follow-up runs.
Every observed client search must return HTTP 200 with usable normalized output.
Every observed built-in search item must have status `completed`. An earlier
success cannot mask a later failed or incomplete search.

The client direct case also requires the fixed target in an actual HTTP `open`
command, reported as `open_target_requested=true`, alongside successful search
calls. This is request-level evidence only. A usable HTTP 200 response can contain
unrelated query results even when the same request includes the target.
Client `open_target_completed` is always null. Neither output text, a result URL,
nor a citation proves that the client opened the page.

The built-in direct case requires a completed `open_page` action with the target in
`action.url`, as described in the [OpenAI web search guide](https://developers.openai.com/api/docs/guides/tools-web-search).
It reports that stronger completion evidence as `open_target_completed=true`.
Its `open_target_requested` remains false because no client HTTP `open` command
is observed.
Unknown actions or an absent target produce `open_not_established`, not a claim
that the backend cannot open pages. Six passing gates do not establish equivalent
direct-open capability between the arms. The follow-up replays the original question
and complete ordered native history, including reasoning and call outputs, with
tools disabled. It must complete with a nonempty answer. Unexpected function
calls fail the gate without execution.

The runner allows at most six arms and twelve turns. Each turn has a 60-second
deadline, each arm has 120 seconds, and the entire invocation has 720 seconds.
The overall deadline takes precedence when it overlaps the final arm's deadline;
the last arm is not guaranteed its full 120 seconds.
The client arm permits up to ten HTTP requests, including four search calls,
five initial response requests, and one follow-up request. The built-in arm
permits two response requests. The total bound is 36 HTTP requests. A test pins
the production search-round and search-call limits to four each. Raising either
limit requires reviewing the budget and this documentation without silently
expanding the authorized 36-request bound.
The runner does not retry. Typed authentication or usage errors stop remaining
trials and mark them `not_run`. Unclassified runtime errors and unexpected
exceptions also stop remaining trials, with generic sanitized error categories
and an overall `incomplete` status. Such errors may conceal authentication or
quota failures. The report cannot attribute them to an upstream or client failure.
Investigate the cause before authorizing another bounded run. Transport errors
raised as `httpx.HTTPError` and timeouts remain evaluated failures and allow
subsequent trials. Other non-200 `/responses` replies, failed or incomplete
streams, and other runtime errors stop the run as incomplete.
Cancellation closes active streams, and overall timeout preserves partial
evidence in the JSON report.

Reports contain counts, booleans, closed outcome and error categories, and elapsed
time to first visible text and completion. They omit raw answers, native items,
URLs, queries, identifiers, backend error messages, and event key names. The
model field contains only the validated CLI identifier. Exit codes are 0 when
all six execution gates pass, 1 for evaluated failure, 2 for missing credentials
or typed authentication or usage blocks, and 3 for an overall timeout or an
unclassified runtime or unexpected error that leaves the comparison incomplete.
Dry-run also exits 0.

This comparison does not assess answer correctness, citation support, Home
Assistant UI, devices, or speech. It reports no statistics or winner. These few
requests and their small timing differences cannot establish a performance or
quality ranking. Synthetic tests exercise current-client routing, native replay,
failure gates, redaction, cancellation, and request bounds without live calls.

For a narrower diagnosis of built-in search followed by native replay, run:

```bash
uv run python scripts/probe_search_comparison.py --model MODEL_ID --builtin-followup-only --dry-run
```

This mode uses only the fixed IANA maintainer case and rejects an explicit
`--first-arm`. It allows at most two HTTP requests and two turns, with 60 seconds
per turn and 120 seconds overall. A request hook blocks a third send. There are
no retries. The existing initial completion, answer, native-item, and search
gates must pass before the tools-disabled follow-up replays the complete native
history in its original order. Payloads and history are not repaired or changed.

The separate diagnostic report records each turn's stage, HTTP status class,
exact HTTP 200 success, decoded-line count, allowlisted event counts, terminal
kind, and natural EOF. Closed outcomes distinguish explicit error, failed, and
incomplete events from empty EOF, undecoded EOF, EOF after events, HTTP rejection,
stream misuse, transport failure, timeout, typed authentication or usage errors,
known probe errors, unattributed runtime errors, and unexpected errors. Exception
messages are never parsed. An unattributed runtime error does not establish
whether the probe, HTTPX, or backend caused the failure. Closure and cancellation
do not count as natural EOF, and completion does not wait for EOF.

Each trace also has `structured_errors`, null unless an `error` or
`response.failed` event was observed. Its bounded list keeps separate `source`
labels for top-level `event` fields, nested `event_error` fields, and
`response_error` fields. A top-level error can retain both identities, without
choosing one over the other. Each identity contains only `source`, `code`, `type`,
and `param`. The envelope `type: error` never becomes an identity type.
Code and type labels use a literal allowlist for authentication, usage limits,
request validation, and server errors in `scripts/search_stream_diagnostic.py`.
These are reported labels, not confirmed causes or a promised backend vocabulary.
Absent fields become `missing`; null, non-string, and unknown fields become
`other`. Missing or malformed error objects report `missing` for each field.
Param labels allow only `input`, `tools`, `model`, `store`, `previous_response_id`,
`include`, and `reasoning`. Paths and all other values become `other`.
An unknown param with a missing code does not establish a history error.
The observer never reads messages, details, or other free-form fields to classify
errors. These labels do not change gates, exit codes, deadlines, or request limits.

Replay evidence comes from the actual outgoing JSON. It contains closed item-type
counts, the first 32 item types and an omitted count, presence counts for fixed
fields, encrypted-reasoning presence and nonempty-string counts, and the actual
encrypted-reasoning include flag, tool count, and store flag. Reports retain no
event bodies, text, identifiers, unallowlisted field values, unknown key names,
headers, URLs, queries, unallowlisted backend codes, exception objects, or
tracebacks. The validated CLI model identifier remains visible. Exit codes remain
0 for passing gates, 1 for evaluated failure, 2 for missing credentials or typed
authentication or usage blocks, and
3 for incomplete or unclassified results. Overall timeout exits 3.

The private observer temporarily wraps the production SSE iterator and delegates
all parsing to it. It yields the same event objects and observes only responses
owned by the diagnostic HTTP client. An exclusive nonblocking scope rejects
overlapping diagnostics before another client or request. Cleanup closes owned
iterators and restores the original iterator on success, failure, timeout, or
cancellation. The normal comparison installs no observer and keeps its report
schema. `tests/test_search_stream_diagnostic.py` verifies these contracts offline
through the real client, HTTPX transports, and CLI subprocesses. These checks do
not establish live backend acceptance or the cause of a previous failed run.
This script-only change needs no integration install or Home Assistant restart.
A new live run requires a separate handoff naming the exact reviewed commit and
the integration-owned credential environment. Offline verification alone does
not authorize that run.

When the hosted-search payload, model defaults, citation handling, or backend contract changes:

1. Run `uv run python scripts/probe_web_search_contract.py --model MODEL_ID --dry-run` and its tests.
   The probe checks the backend's built-in `web_search` tool. The integration does not use that
   tool (it runs search on `alpha/search`, see [ARCHITECTURE.md](ARCHITECTURE.md)), so the probe
   shows whether the built-in tool works again, not whether integration search works.
2. In Home Assistant, enable web search and ask a current-information question that requires search.
3. Check the spoken answer for raw URLs or a source block. Citation metadata in the
   response does not prove a client renders links. The previously inspected stock
   Assist browser ignored the Sources card; test any target client's rendering
   separately rather than treating backend card presence as a UI pass.
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
