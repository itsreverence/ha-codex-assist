# Releasing

This checklist is for maintainers.

## Prepare

1. Update the version in `custom_components/codex_assist/manifest.json` and `pyproject.toml`, then refresh `uv.lock`.
2. Confirm those `X.Y.Z` values match the planned `vX.Y.Z` tag.
3. Review README, wiki, security, support, and compatibility guidance. Check the
   bundled suggestions in `custom_components/codex_assist/codex_models.py` against
   the [official Codex model guidance](https://learn.chatgpt.com/docs/models),
   including retirement notices specific to ChatGPT sign-in. Remove explicitly
   retired suggestions and test replacements before adding them. Update the review
   date beside the list, even when no IDs change. The list contains unverified
   suggestions, not a guarantee of availability for every account. Do not replace
   saved selections as part of this review.
4. Verify brand assets and screenshots contain no private data. For PNG icons, confirm the corner alpha is transparent:

   ```bash
   uv run --with pillow python - <<'PY'
   from pathlib import Path
   from PIL import Image

   for path in [Path("assets/codex-assist-icon.png"), Path("custom_components/codex_assist/brand/icon.png")]:
       image = Image.open(path).convert("RGBA")
       w, h = image.size
       corners = [image.getpixel(point)[3] for point in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]]
       print(path, image.size, corners)
       assert corners == [0, 0, 0, 0]
   PY
   ```

5. When hosted-search requests or search delivery change, complete the compatibility
   checks in [TESTING.md](TESTING.md). Document changes to exported response fields
   in the release notes, including their effect on API consumers.
6. Run all checks in [TESTING.md](TESTING.md).
7. Commit the scoped release change, open a pull request, and wait for every required check to pass.
8. Merge through the protected `main` branch and verify the post-merge `main` checks before tagging.
9. If the release changes user-facing behavior, publish the matching wiki updates after the reviewed repository docs and screenshots are on `main`. Open every changed wiki page once to confirm its links and images resolve.
10. When the wiki carries an upcoming-release notice, remove it as part of this release and rewrite any wording that depends on it. Search the wiki for "Next release" and for the behavior the notice describes, such as `response.card`. Do not leave a notice for a change that has shipped.

## Publish

```bash
git fetch origin main
git tag vX.Y.Z origin/main
git push origin vX.Y.Z
gh release create vX.Y.Z --verify-tag --generate-notes
```

Use reviewed user-facing notes instead of `--generate-notes` when the generated notes are insufficient. Mark beta or release-candidate builds as prereleases.

## Verify

- Confirm all required GitHub Actions checks pass for the exact commit the tag points to.
- Confirm tagged `hacs.json`, `manifest.json`, local brand icon, and source archive are publicly reachable.
- Install or update through HACS and restart Home Assistant.
- Complete the Assist smoke test in [TESTING.md](TESTING.md).
- Verify reauthentication and AI Task behavior when those surfaces changed.

Do not publish screenshots, logs, diagnostics, or release artifacts containing tokens, device codes, cookies, private Home Assistant URLs, or private entity names.
