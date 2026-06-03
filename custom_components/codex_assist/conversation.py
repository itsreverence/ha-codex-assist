from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client

from . import DOMAIN
from .codex_auth import CodexAuthClient
from .codex_client import CodexClient
from .codex_runtime import resolve_runtime_tokens

MAX_TOOL_ITERATIONS = 5
LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CodexAssistConversationEntity(entry)])


class CodexAssistConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
):
    _attr_has_entity_name = True
    _attr_name = "Codex Assist"
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL
    _attr_supports_streaming = False

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | str:
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        settings = {**self.entry.data, **self.entry.options}
        model = settings.get("model", "gpt-5.4")
        prompt = settings.get(
            "prompt",
            "You are a concise Home Assistant Assist conversation agent.",
        )

        response = intent.IntentResponse(language=user_input.language)
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                llm.LLM_API_ASSIST,
                prompt,
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        http_client = get_async_client(self.hass)
        try:
            tokens = await resolve_runtime_tokens(
                self.entry.data,
                auth_client=CodexAuthClient(http_client=http_client),
                async_update_entry_data=lambda data: self.hass.config_entries.async_update_entry(
                    self.entry,
                    data=data,
                ),
            )
        except RuntimeError as err:
            LOGGER.warning("Codex Assist authentication failed; starting reauth flow: %s", err)
            self.entry.async_start_reauth(self.hass)
            response.async_set_speech(
                "Codex Assist needs you to sign in again. Open Home Assistant repairs "
                "or the integration page to reauthenticate."
            )
            return conversation.ConversationResult(
                response=response,
                conversation_id=user_input.conversation_id,
            )

        codex = CodexClient(http_client=http_client, access_token=tokens.access_token)
        try:
            for _iteration in range(MAX_TOOL_ITERATIONS):
                result = await codex.generate_turn(
                    model=model,
                    instructions=_instructions_from_chat_log(chat_log, prompt),
                    input_items=_codex_input_from_chat_log(chat_log),
                    tools=_codex_tools_from_chat_log(chat_log),
                )
                assistant = conversation.AssistantContent(
                    agent_id=user_input.agent_id,
                    content=result.text or None,
                    tool_calls=[
                        llm.ToolInput(
                            id=tool_call.id,
                            tool_name=tool_call.name,
                            tool_args=tool_call.arguments,
                        )
                        for tool_call in result.tool_calls
                    ]
                    or None,
                )

                async for _tool_result in chat_log.async_add_assistant_content(assistant):
                    pass

                if not result.tool_calls:
                    break
        except (httpx.HTTPError, RuntimeError) as err:
            LOGGER.exception("Codex Assist model request failed")
            text = f"Codex Assist failed: {err}"
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=user_input.agent_id,
                    content=text,
                )
            )
        except (ValueError, TypeError) as err:
            LOGGER.exception("Codex Assist tool handling failed")
            text = f"Codex Assist tool handling failed: {err}"
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=user_input.agent_id,
                    content=text,
                )
            )

        return conversation.async_get_result_from_chat_log(user_input, chat_log)


def _instructions_from_chat_log(
    chat_log: conversation.ChatLog,
    fallback_prompt: str,
) -> str:
    for content in chat_log.content:
        if getattr(content, "role", None) == "system" and isinstance(
            getattr(content, "content", None),
            str,
        ):
            return content.content
    return fallback_prompt


def _codex_input_from_chat_log(chat_log: conversation.ChatLog) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    for content in chat_log.content:
        role = getattr(content, "role", None)
        text = getattr(content, "content", None)
        if role == "system":
            continue
        if role == "tool_result":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": content.tool_call_id,
                    "output": json.dumps(content.tool_result),
                }
            )
            continue
        if role in {"user", "assistant"} and isinstance(text, str) and text.strip():
            input_items.append({"role": role, "content": text})

        tool_calls = getattr(content, "tool_calls", None)
        if role == "assistant" and tool_calls:
            for tool_call in tool_calls:
                input_items.append(
                    {
                        "type": "function_call",
                        "name": tool_call.tool_name,
                        "arguments": json.dumps(tool_call.tool_args),
                        "call_id": tool_call.id,
                    }
                )

    return input_items[-24:]


def _codex_tools_from_chat_log(chat_log: conversation.ChatLog) -> list[dict[str, Any]]:
    if not chat_log.llm_api:
        return []

    return [
        _codex_tool_from_ha_tool(tool, chat_log.llm_api.custom_serializer)
        for tool in chat_log.llm_api.tools
    ]


def _codex_tool_from_ha_tool(
    tool: llm.Tool,
    custom_serializer: Any,
) -> dict[str, Any]:
    from voluptuous_openapi import convert  # noqa: PLC0415

    schema = convert(tool.parameters, custom_serializer=custom_serializer)
    unsupported_keys = {"oneOf", "anyOf", "allOf", "enum", "not"}
    if unsupported_keys.intersection(schema):
        schema = {k: v for k, v in schema.items() if k not in unsupported_keys}

    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": schema,
        "strict": False,
    }
