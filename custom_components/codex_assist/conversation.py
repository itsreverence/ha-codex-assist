from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from homeassistant.components import conversation
from homeassistant.components.conversation import AssistantContentDeltaDict
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client

from . import DOMAIN
from .codex_auth import CodexAuthClient, CodexTokenSet
from .codex_client import (
    CodexAuthenticationError,
    CodexClient,
    CodexStreamDelta,
    CodexTextDelta,
    CodexToolCallDelta,
    codex_user_content_with_images,
)
from .codex_runtime import resolve_runtime_tokens
from .config_flow import (
    DEFAULT_REASONING_EFFORT,
    DEFAULT_REASONING_SUMMARY,
    DEFAULT_TEXT_VERBOSITY,
)

MAX_TOOL_ITERATIONS = 5
MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024
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
    _attr_supports_streaming = True

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
        reasoning_effort = settings.get("reasoning_effort", DEFAULT_REASONING_EFFORT)
        reasoning_summary = settings.get("reasoning_summary", DEFAULT_REASONING_SUMMARY)
        text_verbosity = settings.get("text_verbosity", DEFAULT_TEXT_VERBOSITY)

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
        auth_client = CodexAuthClient(http_client=http_client)
        try:
            tokens = await resolve_runtime_tokens(
                self.entry.data,
                auth_client=auth_client,
                async_update_entry_data=lambda data: self.hass.config_entries.async_update_entry(
                    self.entry,
                    data=data,
                ),
            )
        except RuntimeError as err:
            LOGGER.warning("Codex Assist authentication failed; starting reauth flow: %s", err)
            return _start_reauth_result(self.hass, self.entry, response, user_input)

        codex = CodexClient(http_client=http_client, access_token=tokens.access_token)
        try:
            for _iteration in range(MAX_TOOL_ITERATIONS):
                try:
                    tool_call_requested = await _stream_codex_turn_into_chat_log(
                        chat_log=chat_log,
                        codex=codex,
                        entity_id=self.entity_id or "",
                        model=model,
                        instructions=_instructions_from_chat_log(chat_log, prompt),
                        input_items=await _codex_input_from_chat_log(self.hass, chat_log),
                        tools=_codex_tools_from_chat_log(chat_log),
                        reasoning_effort=reasoning_effort,
                        reasoning_summary=reasoning_summary,
                        text_verbosity=text_verbosity,
                    )
                except CodexAuthenticationError as err:
                    LOGGER.warning(
                        "Codex Assist access token was rejected; refreshing and retrying once: %s",
                        err,
                    )
                    try:
                        tokens = await _refresh_runtime_tokens(
                            self.hass,
                            self.entry,
                            auth_client,
                            tokens,
                        )
                    except RuntimeError as refresh_err:
                        LOGGER.warning(
                            "Codex Assist token refresh failed; starting reauth flow: %s",
                            refresh_err,
                        )
                        return _start_reauth_result(
                            self.hass,
                            self.entry,
                            response,
                            user_input,
                        )
                    codex = CodexClient(
                        http_client=http_client,
                        access_token=tokens.access_token,
                    )
                    try:
                        tool_call_requested = await _stream_codex_turn_into_chat_log(
                            chat_log=chat_log,
                            codex=codex,
                            entity_id=self.entity_id or "",
                            model=model,
                            instructions=_instructions_from_chat_log(chat_log, prompt),
                            input_items=await _codex_input_from_chat_log(self.hass, chat_log),
                            tools=_codex_tools_from_chat_log(chat_log),
                            reasoning_effort=reasoning_effort,
                            reasoning_summary=reasoning_summary,
                            text_verbosity=text_verbosity,
                        )
                    except CodexAuthenticationError as retry_err:
                        LOGGER.warning(
                            "Codex Assist token was rejected after refresh; "
                            "starting reauth flow: %s",
                            retry_err,
                        )
                        return _start_reauth_result(
                            self.hass,
                            self.entry,
                            response,
                            user_input,
                        )
                if not tool_call_requested:
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


async def _stream_codex_turn_into_chat_log(
    *,
    chat_log: conversation.ChatLog,
    codex: CodexClient,
    entity_id: str,
    model: str,
    instructions: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    reasoning_effort: str,
    reasoning_summary: str,
    text_verbosity: str,
) -> bool:
    tool_call_requested = False

    def mark_tool_call_requested() -> None:
        nonlocal tool_call_requested
        tool_call_requested = True

    async for _delta in chat_log.async_add_delta_content_stream(
        entity_id,
        _codex_stream_to_assistant_deltas(
            codex.stream_turn(
                model=model,
                instructions=instructions,
                input_items=input_items,
                tools=tools,
                reasoning_effort=reasoning_effort,
                reasoning_summary=reasoning_summary,
                text_verbosity=text_verbosity,
            ),
            on_tool_call=mark_tool_call_requested,
        ),
    ):
        pass
    return tool_call_requested


async def _codex_stream_to_assistant_deltas(
    stream: AsyncIterator[CodexStreamDelta],
    *,
    on_tool_call: Callable[[], None] | None = None,
) -> AsyncIterator[AssistantContentDeltaDict]:
    started = False
    async for delta in stream:
        if not started:
            yield {"role": "assistant"}
            started = True
        if isinstance(delta, CodexTextDelta):
            yield {"content": delta.text}
        elif isinstance(delta, CodexToolCallDelta):
            if on_tool_call is not None:
                on_tool_call()
            yield {
                "tool_calls": [
                    llm.ToolInput(
                        id=delta.tool_call.id,
                        tool_name=delta.tool_call.name,
                        tool_args=delta.tool_call.arguments,
                    )
                ]
            }


async def _refresh_runtime_tokens(
    hass: HomeAssistant,
    entry: ConfigEntry,
    auth_client: CodexAuthClient,
    tokens: CodexTokenSet,
) -> CodexTokenSet:
    if not tokens.refresh_token:
        raise RuntimeError("Codex Assist is missing refresh_token")
    refreshed = await auth_client.refresh(tokens)
    updated_data = dict(entry.data)
    updated_data["access_token"] = refreshed.access_token
    updated_data["refresh_token"] = refreshed.refresh_token
    hass.config_entries.async_update_entry(entry, data=updated_data)
    return refreshed


def _start_reauth_result(
    hass: HomeAssistant,
    entry: ConfigEntry,
    response: intent.IntentResponse,
    user_input: conversation.ConversationInput,
) -> conversation.ConversationResult:
    entry.async_start_reauth(hass)
    response.async_set_speech(
        "Codex Assist needs you to sign in again. Open Home Assistant repairs "
        "or the integration page to reauthenticate."
    )
    return conversation.ConversationResult(
        response=response,
        conversation_id=user_input.conversation_id,
    )


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


async def _codex_input_from_chat_log(
    hass: HomeAssistant,
    chat_log: conversation.ChatLog,
) -> list[dict[str, Any]]:
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
            item_content: str | list[dict[str, Any]] = text
            if role == "user":
                images = await _async_image_attachments_for_codex(
                    hass,
                    getattr(content, "attachments", None),
                )
                item_content = codex_user_content_with_images(text, images)
            input_items.append({"role": role, "content": item_content})

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


async def _async_image_attachments_for_codex(
    hass: HomeAssistant,
    attachments: Any,
) -> list[tuple[str, bytes]]:
    if not attachments:
        return []
    return await hass.async_add_executor_job(_image_attachments_for_codex, attachments)


def _image_attachments_for_codex(attachments: Any) -> list[tuple[str, bytes]]:
    images: list[tuple[str, bytes]] = []
    for attachment in attachments:
        mime_type = getattr(attachment, "mime_type", "")
        if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
            continue
        path = getattr(attachment, "path", None)
        if path is None:
            continue
        try:
            if path.stat().st_size > MAX_IMAGE_ATTACHMENT_BYTES:
                LOGGER.warning(
                    "Skipping Codex Assist image attachment over %s bytes: %s",
                    MAX_IMAGE_ATTACHMENT_BYTES,
                    path,
                )
                continue
            images.append((mime_type, path.read_bytes()))
        except OSError as err:
            LOGGER.warning("Skipping unreadable Codex Assist image attachment %s: %s", path, err)
    return images


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
