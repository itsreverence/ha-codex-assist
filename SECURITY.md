# Security Policy

## Supported versions

Codex Assist is experimental pre-1.0 software. Security fixes are applied to the latest release only.

## Reporting a vulnerability

Please do not open a public issue containing credentials, tokens, cookies, Home Assistant secrets, or private logs.

Report sensitive issues privately to the repository maintainer. Include:

- Home Assistant version
- Codex Assist version or commit
- a minimal description of the issue
- redacted logs or reproduction steps

## Security stance

Codex Assist uses Home Assistant's normal Assist LLM API and exposed-entity controls. It should not add a custom raw service-call bridge or bypass Home Assistant's Assist exposure model.

Keep sensitive entities such as locks, alarms, water shutoff valves, garage doors, covers, and security controls unexposed unless you deliberately want Assist control over them.
