from pathlib import Path

CONVERSATION = Path("custom_components/codex_assist/conversation.py")


def test_conversation_uses_home_assistant_chat_log_history():
    source = CONVERSATION.read_text()

    assert "def _codex_input_from_chat_log" in source
    assert "chat_log.content" in source
    assert 'role in {"user", "assistant"}' in source
    assert '"type": "function_call_output"' in source


def test_conversation_streams_assistant_reply_and_executes_tools_in_chat_log():
    source = CONVERSATION.read_text()

    assert "_attr_supports_streaming = True" in source
    assert "async_add_delta_content_stream" in source
    assert "_codex_stream_to_assistant_deltas" in source
    assert "llm.ToolInput" in source


def test_conversation_enables_full_home_assistant_assist_control():
    source = CONVERSATION.read_text()

    assert "ConversationEntityFeature.CONTROL" in source
    assert "llm.LLM_API_ASSIST" in source
    assert "_codex_tools_from_chat_log" in source


def test_conversation_translates_home_assistant_image_attachments_to_codex_input():
    source = CONVERSATION.read_text()

    assert "_async_image_attachments_for_codex" in source
    assert "mime_type.startswith(\"image/\")" in source
    assert "codex_user_content_with_images" in source
    assert "MAX_IMAGE_ATTACHMENT_BYTES" in source
