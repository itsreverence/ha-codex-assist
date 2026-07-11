# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration that registers a native Assist conversation agent backed by OpenAI Codex / ChatGPT OAuth.

## Guardrails

- Keep the integration generic and publishable; do not bake in personal or home-specific entity IDs.
- Do not copy or persist tokens from other tools (Codex CLI, editors, or other assistants). The integration must own its own OAuth/device-code credentials.
- Treat Codex authentication as based on the official Codex CLI pattern, but document that the downstream Codex service interface is not a stable public third-party API contract.
- Follow Home Assistant's native Assist LLM API/tool path for voice-model control; do not invent a raw arbitrary service-call bridge.
- Avoid public endpoints. All auth and model calls should happen inside the user's HA instance.

## Verification

- Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and pull-request expectations.
- Run `uv run pytest -q` and `uv run ruff check .` before public-quality handoff.
- Use [docs/TESTING.md](docs/TESTING.md) for live Home Assistant and feature-specific smoke tests.
