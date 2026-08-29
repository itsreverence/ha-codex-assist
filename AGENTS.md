# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration backed by OpenAI Codex / ChatGPT OAuth. The integration provides a native Assist conversation agent, a native AI Task provider, and optional hosted web search with validated visual citations.

## Guardrails

- Keep the integration generic and publishable. Do not add personal or home-specific entity IDs.
- Do not copy or persist tokens from other tools such as Codex CLI, editors, or other assistants. The integration must own its OAuth and device-code credentials.
- Follow the official Codex CLI authentication pattern, but document that the downstream Codex service interface is not a stable public third-party API contract.
- Route device control through Home Assistant's native Assist LLM API. Do not add a raw arbitrary service-call bridge.
- Keep hosted web search opt-in. Preserve validated citations for display and instruct the model to keep raw URLs out of spoken output.
- Use Home Assistant's native AI Task types for structured data, attachments, and generated images. Do not add a separate upload service.
- Avoid public endpoints. Authentication and model calls should happen inside the user's Home Assistant instance.
- Never commit tokens, device codes, cookies, private URLs, private entity names, or unredacted screenshots and logs.

## Canonical docs

- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing data flow, authentication, tool routing, hosted search, or AI Task behavior.
- Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and pull-request expectations.
- Use [docs/TESTING.md](docs/TESTING.md) for local, Home Assistant contract, and live smoke tests.
- Use [docs/RELEASING.md](docs/RELEASING.md) for versioning, release checks, and public artifact verification.
- Update the README and wiki when setup, options, compatibility, screenshots, or user-visible behavior changes.

## Verification

- Run `uv run ruff check .` and `uv run pytest -q` before public-quality handoff.
- Run both Home Assistant contract environments when integration behavior or dependency support changes.
- Check every new screenshot for private hosts, account details, entity names, tokens, and device codes before publishing it.
