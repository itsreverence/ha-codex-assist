# Workflow

## Local checks

Run these before public handoff:

```bash
uv run ruff check .
uv run pytest
```

## HACS release checklist

1. Update `custom_components/codex_assist/manifest.json` version.
2. Confirm the manifest version matches the GitHub tag you plan to publish.
3. Verify the README, wiki links, and local brand assets still render cleanly.
4. Run local checks.
5. Commit the scoped change.
6. Tag and push the release:

   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

7. Create the GitHub release.
8. Confirm GitHub Actions passes for the pushed commit/tag.
9. Verify raw release files are reachable:
   - `hacs.json`
   - `custom_components/codex_assist/manifest.json`
   - `custom_components/codex_assist/brand/icon.png`
   - source zip for the tag
10. Install/update through HACS and restart Home Assistant.
11. Smoke test one harmless Assist command after restart.

## Visual asset checks

For PNG icons, verify they do not include a baked opaque canvas:

```bash
python - <<'PY'
from PIL import Image
for path in ["assets/codex-assist-icon.png", "custom_components/codex_assist/brand/icon.png"]:
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    print(path, im.size, [im.getpixel(xy) for xy in [(0,0), (w-1,0), (0,h-1), (w-1,h-1)]])
PY
```

Transparent icon corners should report alpha `0`.

For generated documentation diagrams, prefer PNG/JPEG/WEBP for image-analysis tooling and GitHub preview compatibility. Raw SVG is fine for hand-authored vector diagrams, but render it to PNG before using image/vision tooling.

Do not publish screenshots that expose tokens, device codes, cookies, private Home Assistant URLs, private entity names, or private dashboard URLs.

## Unreleased branch test install

Use this for local testing only. Prefer this over cutting beta releases while a HACS default PR is waiting for review.

1. Download the branch archive:

   ```text
   https://github.com/itsreverence/ha-codex-assist/archive/refs/heads/v0.2-media-ai-task.zip
   ```

2. Extract the zip locally.
3. Copy the extracted `custom_components/codex_assist` directory into Home Assistant:

   ```text
   /config/custom_components/codex_assist
   ```

4. Restart Home Assistant.
5. Confirm the integration version/logs reflect the branch code before testing. The `v0.2-media-ai-task` branch uses a beta manifest version such as `0.2.0-beta.1`; `main` remains on the latest stable `0.1.x` release for HACS review.

To roll back, reinstall the latest stable release from HACS and restart Home Assistant.

## Post-install smoke test

After a Home Assistant restart:

1. Confirm `conversation.codex_assist` exists.
2. Select **Codex Assist** in an Assist pipeline.
3. Ask a normal read-only question.
4. Ask it to list exposed entities.
5. Test one harmless exposed light on/off.
6. Confirm sensitive entities remain unexposed unless intentionally allowed.

## Image attachment smoke test

Use this only after installing the unreleased `v0.2-media-ai-task` branch.

This is a defensive backend check, not a promise that the normal Home Assistant Assist pop-up has an upload button. Home Assistant conversation chat logs can carry `UserContent.attachments`, and provider integrations may translate them if they appear, but the public `conversation.process` schema does not currently accept attachments and `ConversationEntityFeature` has no attachment flag. Prefer AI Task for native attachment workflows.

1. Reauth Codex Assist if Home Assistant shows a repair or auth failure.
2. Select an Assist/chat surface that exposes attachment upload for conversation agents. Home Assistant's normal voice Assist pop-up may not show an upload button; that means the UI surface cannot exercise conversation attachments directly, not necessarily that the integration backend is missing image support.
3. Attach a small PNG/JPEG image under 10 MB if the surface exposes an upload control.
4. Ask a visual question such as:

   ```text
   What is in this image? Do not control any devices.
   ```

5. Confirm the answer references actual image content.
6. Confirm Home Assistant logs do not show `codex_assist` attachment/read errors.
7. Try the same prompt without an image and confirm normal text-only Assist still works.

If the Assist UI you are using has no attachment button, the backend support is present but that UI surface cannot exercise it directly. Home Assistant AI Task has explicit `SUPPORT_ATTACHMENTS` feature support, so AI Task is the preferred native surface for future image/PDF smoke tests.

## AI Task attachment smoke test

Use this for the native HA attachment path on the `v0.2-media-ai-task` branch.

1. Restart Home Assistant after installing the branch.
2. Confirm the integration exposes a Codex Assist AI Task entity.
3. Call `ai_task.generate_data` with the Codex Assist AI Task entity, simple instructions, and a local media/camera/image attachment.
4. Confirm the result references the attachment content.
5. Confirm an attachment request is accepted because the entity advertises `AITaskEntityFeature.SUPPORT_ATTACHMENTS`.
6. Confirm logs do not print tokens, local file contents, or base64 payloads.

Do not advertise `GENERATE_IMAGE` unless Codex Assist intentionally supports image generation output. For v0.2, the intended scope is data generation from text plus image attachments.

## Reauth smoke test

When changing auth or before a public release that touches token handling:

1. Confirm an expired/invalidated token produces a clear reauth path instead of a silent failure.
2. Complete device-code sign-in again.
3. Confirm the integration resumes without recreating unrelated Home Assistant configuration.
4. Confirm logs do not print tokens, refresh tokens, cookies, or device codes.

## Model selector smoke test

When changing model configuration or before a release that includes model selector changes:

1. Start setup/options without a usable token and confirm the curated fallback models appear.
2. Complete auth and reopen options.
3. Confirm backend-discovered models appear when the Codex backend returns them.
4. Confirm a custom model slug can still be retained or entered manually.
5. Confirm failed model discovery falls back gracefully and does not block setup.
