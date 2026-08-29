import httpx

from custom_components.codex_assist.error_formatting import request_failure_text


def test_request_failure_text_uses_exception_type_for_blank_transport_error():
    assert request_failure_text("Codex Assist AI Task failed", httpx.ReadTimeout("")) == (
        "Codex Assist AI Task failed: ReadTimeout"
    )


def test_request_failure_text_preserves_nonblank_detail():
    assert request_failure_text("Codex Assist failed", RuntimeError("backend failed")) == (
        "Codex Assist failed: backend failed"
    )
