# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration that registers a native Assist conversation agent backed by OpenAI Codex / ChatGPT OAuth.

## Guardrails

- Keep the integration generic and publishable; do not bake in Larry/Ricky Home entity IDs.
- Do not copy or persist Hermes tokens. The integration must own its own OAuth/device-code credentials.
- Treat Codex authentication as based on the official Codex CLI pattern, but document that the downstream Codex service interface is not a stable public third-party API contract.
- Follow Home Assistant's native Assist LLM API/tool path for voice-model control; do not invent a raw arbitrary service-call bridge.
- Avoid public endpoints. All auth and model calls should happen inside the user's HA instance.

## Verification

- Use tests for token refresh, payload conversion, and conversation response extraction.
- Run `uv run pytest -q` before claiming success.
- Run `uv run ruff check .` before public-quality handoff.
