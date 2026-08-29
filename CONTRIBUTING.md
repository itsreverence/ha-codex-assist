# Contributing to Codex Assist

Thanks for helping improve Codex Assist. Bug fixes, documentation improvements, compatibility fixes, translations, and focused feature proposals are welcome.

## Before you start

- Search existing issues before opening a new one.
- Use an issue for substantial behavior changes so the scope and Home Assistant compatibility can be discussed first.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- Never include tokens, device codes, cookies, Home Assistant secrets, private URLs, config entries, or unredacted logs and screenshots.

## Development setup

Install [uv](https://docs.astral.sh/uv/) and use the repository-managed environment:

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run pytest -q
```

The fast suite under `tests/` uses lightweight Home Assistant fakes. Run the real Home Assistant harness in an isolated Python 3.14 environment so it does not reuse the project's normal environment:

```bash
uv run --isolated --python 3.14 --with-requirements requirements_test_ha.txt \
  python -m pytest tests_ha -q
```

## Pull requests

Keep changes narrowly scoped and explain:

- the user-visible problem or benefit;
- compatibility or security implications;
- tests added or updated;
- the commands you ran.

Update user documentation when setup, options, compatibility, or safety behavior changes. Do not mix unrelated cleanup into a functional change.

For live Home Assistant verification, see [docs/TESTING.md](docs/TESTING.md). Maintainers use [docs/RELEASING.md](docs/RELEASING.md) for releases.

## Project boundaries

Codex Assist must continue to route device control through Home Assistant's Assist LLM API and exposed-entity controls. It must not add an unrestricted service-call bridge, reuse credentials from unrelated tools, or require a separate public-facing Codex server.
