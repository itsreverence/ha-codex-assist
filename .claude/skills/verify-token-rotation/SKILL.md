---
name: verify-token-rotation
description: Exhaustively check Codex Assist's refresh-token rotation (RuntimeTokenCoordinator, its callers in conversation, AI Task and model discovery, and the reauth/reload path) under every interleaving of its await points. Use when a change touches codex_runtime.py, how callers resolve or refresh tokens, config-entry reload or reauth, or entry.runtime_data.
---

Read and follow `.agents/skills/verify-token-rotation/SKILL.md`. It is the canonical copy, shared with Codex and other agents; edit that file, not this one.
