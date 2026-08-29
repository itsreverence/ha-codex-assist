"""Consistent user-facing error formatting for Codex Assist surfaces."""

from __future__ import annotations


def request_failure_text(prefix: str, err: BaseException) -> str:
    """Return a useful failure message even when a transport error is blank."""
    detail = str(err).strip() or type(err).__name__
    return f"{prefix}: {detail}"
