from pathlib import Path

CONVERSATION = Path("custom_components/codex_assist/conversation.py")


def test_conversation_uses_home_assistant_chat_log_history():
    source = CONVERSATION.read_text()

    assert "def _codex_messages_from_chat_log" in source
    assert "chat_log.content" in source
    assert "CodexMessage(role=\"assistant\"" in source
    assert "CodexMessage(role=\"user\"" in source


def test_conversation_records_assistant_reply_in_home_assistant_chat_log():
    source = CONVERSATION.read_text()

    assert "async_add_assistant_content_without_tools" in source
    assert "conversation.AssistantContent" in source
    assert "agent_id=user_input.agent_id" in source


def test_conversation_keeps_control_feature_disabled_until_llm_api_mode_exists():
    source = CONVERSATION.read_text()

    assert "ConversationEntityFeature.CONTROL" not in source
    assert "_attr_supported_features = conversation.ConversationEntityFeature(0)" in source
