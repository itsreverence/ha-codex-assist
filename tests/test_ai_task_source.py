from pathlib import Path

AI_TASK_SOURCE = Path("custom_components/codex_assist/ai_task.py").read_text()
CONVERSATION_SOURCE = Path("custom_components/codex_assist/conversation.py").read_text()


def test_ai_task_declares_native_attachment_and_image_generation_support():
    assert "ai_task.AITaskEntity" in AI_TASK_SOURCE
    assert "AITaskEntityFeature.GENERATE_DATA" in AI_TASK_SOURCE
    assert "AITaskEntityFeature.GENERATE_IMAGE" in AI_TASK_SOURCE
    assert "AITaskEntityFeature.SUPPORT_ATTACHMENTS" in AI_TASK_SOURCE
    assert "_async_generate_image" in AI_TASK_SOURCE
    assert "DEFAULT_IMAGE_MODEL" in AI_TASK_SOURCE
    assert "DEFAULT_IMAGE_SIZE" in AI_TASK_SOURCE


def test_ai_task_uses_ha_chat_log_attachment_path():
    assert "ai_task.GenDataTask" in AI_TASK_SOURCE
    assert "_codex_input_from_chat_log" in AI_TASK_SOURCE
    assert "task.structure" in AI_TASK_SOURCE
    assert "GenDataTaskResult" in AI_TASK_SOURCE


def test_conversation_attachment_handling_remains_defensive_only():
    assert "getattr(content, \"attachments\", None)" in CONVERSATION_SOURCE
    assert "_async_image_attachments_for_codex" in CONVERSATION_SOURCE
    assert "ConversationEntityFeature.CONTROL" in CONVERSATION_SOURCE
    assert "SUPPORT_ATTACHMENTS" not in CONVERSATION_SOURCE
