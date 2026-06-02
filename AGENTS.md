# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration that registers a native Assist conversation agent backed by OpenAI Codex / ChatGPT OAuth.

## Guardrails

- Keep the integration generic and publishable; do not bake in Larry/Ricky Home entity IDs.
- Do not copy or persist Hermes tokens. The integration must own its own OAuth/device-code credentials.
- Treat Codex backend access as experimental/unsupported by OpenAI's public API docs; document that clearly.
- Default to safe Home Assistant behavior: text-only first, then curated HA LLM API control behind explicit options.
- Never expose broad/raw Home Assistant control by default.
- Avoid public endpoints. All auth and model calls should happen inside the user's HA instance.

## Verification

- Use tests for token refresh, payload conversion, and conversation response extraction.
- Run `uv run pytest -q` before claiming success.
- Run `uv run ruff check .` before public-quality handoff.
