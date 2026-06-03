from pathlib import Path

CONVERSATION = Path("custom_components/codex_assist/conversation.py")


def test_conversation_uses_runtime_token_resolution_before_codex_request():
    source = CONVERSATION.read_text()

    assert "resolve_runtime_tokens" in source
    assert "CodexAuthClient" in source
    assert "async_update_entry" in source
    assert "tokens.access_token" in source
    assert "**self.entry.options" in source


def test_conversation_auth_error_starts_reauth_instead_of_old_placeholder_message():
    source = CONVERSATION.read_text()

    assert "OAuth login is not wired yet" not in source
    assert "async_start_reauth" in source
    assert "needs you to sign in again" in source
