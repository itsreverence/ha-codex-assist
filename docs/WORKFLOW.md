# Workflow

## Local checks

Run these before public handoff:

```bash
uv run ruff check .
uv run pytest
```

## HACS release checklist

1. Update `custom_components/codex_assist/manifest.json` version.
2. Verify the README and local brand assets still render cleanly.
3. Run local checks.
4. Commit the scoped change.
5. Tag and push the release:

   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

6. Create the GitHub release.
7. Verify raw release files are reachable:
   - `hacs.json`
   - `custom_components/codex_assist/manifest.json`
   - `custom_components/codex_assist/brand/icon.png`
   - source zip for the tag
8. Install/update through HACS and restart Home Assistant.
9. Smoke test one harmless Assist command after restart.

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

## Post-install smoke test

After a Home Assistant restart:

1. Confirm `conversation.codex_assist` exists.
2. Select **Codex Assist** in an Assist pipeline.
3. Ask a normal read-only question.
4. Ask it to list exposed entities.
5. Test one harmless exposed light on/off.
6. Confirm sensitive entities remain unexposed unless intentionally allowed.
