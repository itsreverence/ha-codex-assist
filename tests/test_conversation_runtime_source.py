from pathlib import Path

CONVERSATION = Path("custom_components/codex_assist/conversation.py")


def test_conversation_uses_runtime_token_resolution_before_codex_request():
    source = CONVERSATION.read_text()

    assert "resolve_runtime_tokens" in source
    assert "CodexAuthClient" in source
    assert "async_update_entry" in source
    assert "tokens.access_token" in source
    assert "**self.entry.options" in source


def test_conversation_missing_token_message_reflects_setup_problem_not_unwired_oauth():
    source = CONVERSATION.read_text()

    assert "OAuth login is not wired yet" not in source
    assert "missing Codex access token" in source
