# Testing

## Automated checks

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run pytest -q
```

The fast suite under `tests/` uses lightweight Home Assistant fakes. Run the `tests_ha/` suite in an isolated Python 3.14 environment so it does not reuse the normal project environment:

```bash
uv run --isolated --python 3.14 --with-requirements requirements_test_ha.txt \
  python -m pytest tests_ha -q

uv run --isolated --python 3.14 --with-requirements requirements_test_ha_min.txt \
  python -m pytest tests_ha -q
```

CI runs the fast suite plus pinned real-Home-Assistant contract lanes against
Home Assistant 2026.8.3 and Home Assistant 2026.6.0, the minimum supported
version. Update the stable pins in `requirements_test_ha.txt` together when
advancing that contract. Both lanes also run weekly to catch regressions without
silently resolving a prerelease or a newly incompatible test harness mid-run.

## Hosted-search compatibility

When the hosted-search payload, model defaults, citation handling, or backend contract changes:

1. Run `uv run python scripts/probe_web_search_contract.py --dry-run` and its tests.
2. In Home Assistant, enable web search and ask a current-information question that requires search.
3. Verify the displayed answer includes validated clickable citations and the spoken answer contains no raw URLs or source block.
4. Verify a long spoken answer completes without a new Codex Assist or audio error.
5. If an integration-owned OAuth token is available, run the sanitized live probe:

   ```bash
   CODEX_ASSIST_ACCESS_TOKEN='[ephemeral integration-owned token]' \
     uv run python scripts/probe_web_search_contract.py
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
- verify a stale saved model falls back safely;
- verify discovery failure does not block setup.

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
