from pathlib import Path

CONVERSATION = Path("custom_components/codex_assist/conversation.py")


def test_conversation_uses_home_assistant_chat_log_history():
    source = CONVERSATION.read_text()

    assert "def _codex_input_from_chat_log" in source
    assert "chat_log.content" in source
    assert 'role in {"user", "assistant"}' in source
    assert '"type": "function_call_output"' in source


def test_conversation_records_assistant_reply_and_executes_tools_in_chat_log():
    source = CONVERSATION.read_text()

    assert "async_add_assistant_content(assistant)" in source
    assert "conversation.AssistantContent" in source
    assert "agent_id=user_input.agent_id" in source
    assert "llm.ToolInput" in source


def test_conversation_enables_full_home_assistant_assist_control():
    source = CONVERSATION.read_text()

    assert "ConversationEntityFeature.CONTROL" in source
    assert "llm.LLM_API_ASSIST" in source
    assert "_codex_tools_from_chat_log" in source
