# Security Policy

## Supported versions

Codex Assist is experimental pre-1.0 software. Security fixes are applied to the latest release only.

## Reporting a vulnerability

Please do not open a public issue containing credentials, tokens, cookies, Home Assistant secrets, private URLs, or private logs.

Use GitHub's private vulnerability reporting / security advisory flow if it is available for this repository. If private reporting is not available, contact the repository maintainer privately before sharing sensitive details.

Include:

- Home Assistant version
- Codex Assist version or commit
- a minimal description of the issue
- redacted logs or reproduction steps

Do not include raw access tokens, refresh tokens, device codes, cookies, full Home Assistant config entries, or screenshots containing private URLs.

## Security stance

Codex Assist uses Home Assistant's normal Assist LLM API and exposed-entity controls. It does not add a custom raw service-call bridge or bypass Home Assistant's Assist exposure model.

The diagram below covers the device-control path. Hosted search and AI Task have separate data paths described after it.

![Codex Assist device-control safety model](assets/codex-assist-safety-model.png)

Codex or ChatGPT may request an action, but Codex Assist routes that request through Home Assistant's Assist LLM API. Home Assistant limits execution to the entities exposed to Assist.

Hosted web search is disabled by default. When enabled, the Codex backend may use the current model turn, including the conversation context already supplied for Assist, to formulate a search. Codex Assist does not add a separate location feed or bypass Home Assistant's exposed-entity boundary. Citation links are accepted only from structured backend annotations and must use validated HTTP(S) URLs before they are displayed.

AI Task prompts and supported image attachments are sent to the Codex backend when you run those tasks. Attachment handling is limited to image files, at most four files, 10 MiB per file, and 20 MiB total. Generated images are returned through Home Assistant's native AI Task result type. Structured tasks keep hosted web search disabled so citation text cannot corrupt schema-constrained output.

## Entity exposure guidance

Only expose entities you intentionally want an Assist conversation agent to read or control.

Be especially careful with:

- locks
- alarms
- garage doors
- covers and gates
- water shutoff valves
- security cameras and security controls
- HVAC modes or setpoints that could create safety or cost issues
- scripts, scenes, buttons, or switches that trigger broad automations

If in doubt, keep the entity unexposed and test with harmless read-only entities or lights first.

## Credential handling

If a Codex / ChatGPT token, Home Assistant token, cookie, device code, or private Home Assistant URL is exposed:

1. revoke or rotate the affected credential;
2. re-authenticate the integration if needed;
3. remove the sensitive material from logs, screenshots, issues, and commits;
4. avoid posting the raw secret in public follow-up discussion.

Codex Assist stores authentication material in Home Assistant's config entry storage. Treat Home Assistant backups, diagnostics, and logs as sensitive unless you have reviewed and redacted them.
