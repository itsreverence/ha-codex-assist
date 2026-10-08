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
and image bytes plus configured metadata reaching HA's media store. They do
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

Replace `MODEL_ID` below with a model from the integration’s account-discovered
list. The probe requires an explicit model so it cannot silently test a retired
hardcoded default.

When the hosted-search payload, model defaults, citation handling, or backend contract changes:

1. Run `uv run python scripts/probe_web_search_contract.py --model MODEL_ID --dry-run` and its tests.
2. In Home Assistant, enable web search and ask a current-information question that requires search.
3. Verify the displayed answer includes validated clickable citations and the spoken answer contains no raw URLs or source block.
4. Verify a long spoken answer completes without a new Codex Assist or audio error.
5. If an integration-owned OAuth token is available, run the sanitized live probe:

   ```bash
   CODEX_ASSIST_ACCESS_TOKEN='[ephemeral integration-owned token]' \
     uv run python scripts/probe_web_search_contract.py --model MODEL_ID
   ```

   The probe emits event names and key shapes, not response text, search queries,
   URLs, identifiers, or credentials. Never borrow credentials from Codex CLI,
   an editor, or another assistant.

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
a 20 MiB aggregate limit per request. Requests over the count or aggregate limit fail
instead of silently discarding attachments.

Before publishing screenshots, remove private URLs, account details, tokens, device codes, and private entity or dashboard names.
