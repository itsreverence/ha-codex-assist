from pathlib import Path

CONVERSATION = Path("custom_components/codex_assist/conversation.py")


def test_conversation_starts_reauth_when_runtime_tokens_fail():
    source = CONVERSATION.read_text()

    assert "async_start_reauth" in source
    assert "Codex Assist authentication failed; starting reauth flow" in source
    assert "needs you to sign in again" in source


def test_conversation_logs_model_and_tool_failures():
    source = CONVERSATION.read_text()

    assert "LOGGER.exception(\"Codex Assist model request failed\")" in source
    assert "LOGGER.exception(\"Codex Assist tool handling failed\")" in source
