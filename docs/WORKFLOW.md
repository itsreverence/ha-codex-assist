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

## Post-install smoke test

After a Home Assistant restart:

1. Confirm `conversation.codex_assist` exists.
2. Select **Codex Assist** in an Assist pipeline.
3. Ask a normal read-only question.
4. Ask it to list exposed entities.
5. Test one harmless exposed light on/off.
6. Confirm sensitive entities remain unexposed unless intentionally allowed.

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
