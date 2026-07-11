# Releasing

This checklist is for maintainers.

## Prepare

1. Update `custom_components/codex_assist/manifest.json`.
2. Confirm its version matches the planned `vX.Y.Z` tag.
3. Review README, wiki, security, support, and compatibility guidance.
4. Verify brand assets and screenshots contain no private data.
5. Run all checks in [TESTING.md](TESTING.md).
6. Commit the scoped release change and wait for CI to pass.

## Publish

```bash
git tag vX.Y.Z
git push origin main vX.Y.Z
```

Create the GitHub release from the tag with user-facing notes. Mark beta or release-candidate builds as prereleases.

## Verify

- Confirm GitHub Actions passes for the exact commit and tag.
- Confirm tagged `hacs.json`, `manifest.json`, local brand icon, and source archive are publicly reachable.
- Install or update through HACS and restart Home Assistant.
- Complete the Assist smoke test in [TESTING.md](TESTING.md).
- Verify reauthentication and AI Task behavior when those surfaces changed.

Do not publish screenshots, logs, diagnostics, or release artifacts containing tokens, device codes, cookies, private Home Assistant URLs, or private entity names.
