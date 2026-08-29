# Codex Assist for Home Assistant

<p align="center">
  <img src="assets/codex-assist-icon.png" alt="Codex Assist icon" width="128" height="128">
</p>

<p align="center">
  <a href="https://github.com/itsreverence/ha-codex-assist/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/itsreverence/ha-codex-assist?style=for-the-badge"></a>
  <a href="https://github.com/itsreverence/ha-codex-assist/actions"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/itsreverence/ha-codex-assist/ci.yml?branch=main&style=for-the-badge&label=CI"></a>
  <a href="https://www.hacs.xyz/docs/use/repositories/dashboard/"><img alt="Available in HACS" src="https://img.shields.io/badge/HACS-Default-41BDF5?style=for-the-badge"></a>
</p>

Use OpenAI Codex / ChatGPT as a Home Assistant Assist conversation agent and AI Task provider.

Codex Assist signs in with Codex-style ChatGPT device-code auth. It keeps device control inside Home Assistant's normal exposed-entity safety model and does not require an OpenAI API key.

> Experimental: this project is not affiliated with OpenAI or Home Assistant. Codex backend compatibility may change with upstream Codex updates.

## Quick install

Requirements: Home Assistant `2026.6.0` or newer, HACS, and a ChatGPT account or plan with Codex access.

[![Open HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=itsreverence&repository=ha-codex-assist&category=integration)
[![Add the Codex Assist integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=codex_assist)

1. In Home Assistant, open **HACS → Integrations**.
2. Search for **Codex Assist**, open it, and select **Download**.
3. Restart Home Assistant.
4. In ChatGPT, enable **Settings → Security → Enable device code authorization for Codex** if it is available on your account.
5. Go to **Settings → Devices & services → Add integration**, or use the button above.
6. Search for **Codex Assist** and complete device-code sign-in.
7. Select **Codex Assist** in your Assist pipeline.
8. Test with a harmless exposed entity first, such as a single light.

Codex Assist is included in HACS by default. You do not need to add this repository as a custom repository. If it does not appear immediately after a release or catalog change, refresh HACS data and try again later.

## What it does

### Assist conversations

- Registers as a normal Home Assistant Assist conversation agent.
- Streams replies while Codex is answering.
- Reads and controls only the entities Home Assistant exposes to Assist.
- Supports simple follow-up actions through Home Assistant's native Assist LLM API.
- Can optionally use hosted web search. Search is off by default. Visible results keep validated source links in a separate card, while the integration instructs the model to omit raw URLs and source blocks from spoken responses.

### AI Task

- Generates plain text or structured data from Home Assistant AI Task actions.
- Accepts supported image attachments on AI Task surfaces.
- Generates images with curated quality and size controls.
- Disables hosted web search for schema-constrained tasks so citation text cannot corrupt structured output.

### Options

Choose the model, system prompt, reasoning effort, reasoning summary, text verbosity, hosted web search, image quality, and image size from the integration options flow.

<p align="center">
  <img src="assets/codex-assist-options.png" alt="Codex Assist options for model, reasoning, web search, and image generation" width="430">
</p>

<p align="center">
  <img src="assets/codex-assist-light-control.png" alt="Codex Assist confirming yard lights are on and turning them off" width="430">
</p>

## Safety short version

Codex Assist does **not** expose a raw “call any Home Assistant service” bridge. It routes control through Home Assistant's Assist LLM API, so your **Assist exposed entities** list is the practical safety boundary.

Start with harmless lights or read-only questions. Keep locks, alarms, garage doors, water shutoff valves, covers, and other sensitive devices unexposed unless you deliberately want Assist control there.

Hosted web search sends the current model turn to the Codex backend when enabled. AI Task prompts and supported attachments are also sent to the backend when you run those tasks. See the [security policy](SECURITY.md) for the full data and control boundaries.

## User guide

The GitHub Wiki is the main user manual:

- [Installation](https://github.com/itsreverence/ha-codex-assist/wiki/Installation)
- [Features and Options](https://github.com/itsreverence/ha-codex-assist/wiki/Features-and-Options)
- [Choosing a Model](https://github.com/itsreverence/ha-codex-assist/wiki/Choosing-a-Model)
- [Safe Entity Exposure](https://github.com/itsreverence/ha-codex-assist/wiki/Safe-Entity-Exposure)
- [Troubleshooting](https://github.com/itsreverence/ha-codex-assist/wiki/Troubleshooting)
- [Compatibility and Limitations](https://github.com/itsreverence/ha-codex-assist/wiki/Compatibility-and-Limitations)

## Project docs

- [User support](SUPPORT.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Testing](docs/TESTING.md)
- [Release process](docs/RELEASING.md)
