from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from homeassistant.components import ai_task, conversation
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util.json import json_loads

from .codex_auth import CodexAuthClient, CodexTokenSet
from .codex_client import CodexAuthenticationError, CodexClient, CodexImageResult
from .codex_runtime import resolve_runtime_tokens
from .config_flow import (
    DEFAULT_REASONING_EFFORT,
    DEFAULT_REASONING_SUMMARY,
    DEFAULT_TEXT_VERBOSITY,
)
from .conversation import (
    MAX_TOOL_ITERATIONS,
    _codex_input_from_chat_log,
    _codex_tools_from_chat_log,
    _instructions_from_chat_log,
    _refresh_runtime_tokens,
    _stream_codex_turn_into_chat_log,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CodexAssistAITaskEntity(entry)])


class CodexAssistAITaskEntity(ai_task.AITaskEntity):
    """AI Task entity for Codex Assist.

    Home Assistant AI Task explicitly supports attachment inputs through the
    SUPPORT_ATTACHMENTS feature flag. Normal Assist conversation surfaces may
    carry chat-log attachments internally, but they do not expose an equivalent
    conversation feature flag or process-service attachment schema.
    """

    _attr_has_entity_name = True
    _attr_name = "Codex Assist AI Task"
    _attr_supported_features = (
        ai_task.AITaskEntityFeature.GENERATE_DATA
        | ai_task.AITaskEntityFeature.GENERATE_IMAGE
        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ai_task"

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Generate data from instructions and optional HA-native attachments."""
        settings = {**self.entry.data, **self.entry.options}
        model = settings.get("model", "gpt-5.4")
        prompt = settings.get(
            "prompt",
            "You are a concise Home Assistant AI Task agent.",
        )
        reasoning_effort = settings.get("reasoning_effort", DEFAULT_REASONING_EFFORT)
        reasoning_summary = settings.get("reasoning_summary", DEFAULT_REASONING_SUMMARY)
        text_verbosity = settings.get("text_verbosity", DEFAULT_TEXT_VERBOSITY)

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
            LOGGER.warning("Codex Assist AI Task authentication failed: %s", err)
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Codex Assist needs you to sign in again. Open Home Assistant "
                "repairs or the integration page to reauthenticate."
            ) from err

        codex = CodexClient(http_client=http_client, access_token=tokens.access_token)
        try:
            await _run_codex_ai_task_chat_log(
                hass=self.hass,
                entry=self.entry,
                auth_client=auth_client,
                tokens=tokens,
                codex=codex,
                chat_log=chat_log,
                entity_id=self.entity_id or "",
                model=model,
                prompt=prompt,
                reasoning_effort=reasoning_effort,
                reasoning_summary=reasoning_summary,
                text_verbosity=text_verbosity,
            )
        except (httpx.HTTPError, RuntimeError) as err:
            LOGGER.exception("Codex Assist AI Task model request failed")
            raise HomeAssistantError(f"Codex Assist AI Task failed: {err}") from err
        except (ValueError, TypeError) as err:
            LOGGER.exception("Codex Assist AI Task response handling failed")
            raise HomeAssistantError(
                f"Codex Assist AI Task response handling failed: {err}"
            ) from err

        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            raise HomeAssistantError("Codex Assist AI Task did not produce a response")

        text = chat_log.content[-1].content or ""
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id,
            data=_structured_data_from_text(text, task.structure),
        )

    async def _async_generate_image(
        self,
        task: ai_task.GenImageTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenImageTaskResult:
        """Generate an image from instructions and optional HA-native attachments."""
        settings = {**self.entry.data, **self.entry.options}
        chat_model = settings.get("model", "gpt-5.4")
        image_model = settings.get("image_model", "gpt-image-2-medium")

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
            LOGGER.warning("Codex Assist AI Task authentication failed: %s", err)
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Codex Assist needs you to sign in again. Open Home Assistant "
                "repairs or the integration page to reauthenticate."
            ) from err

        codex = CodexClient(http_client=http_client, access_token=tokens.access_token)
        try:
            result = await _generate_codex_ai_task_image(
                hass=self.hass,
                entry=self.entry,
                auth_client=auth_client,
                tokens=tokens,
                codex=codex,
                chat_log=chat_log,
                task=task,
                chat_model=chat_model,
                image_model=image_model,
            )
        except (httpx.HTTPError, RuntimeError) as err:
            LOGGER.exception("Codex Assist AI Task image request failed")
            raise HomeAssistantError(f"Codex Assist image generation failed: {err}") from err
        except (ValueError, TypeError) as err:
            LOGGER.exception("Codex Assist AI Task image response handling failed")
            raise HomeAssistantError(
                f"Codex Assist image response handling failed: {err}"
            ) from err

        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=self.entity_id,
                content=result.revised_prompt or "",
            )
        )

        return ai_task.GenImageTaskResult(
            image_data=result.image_data,
            conversation_id=chat_log.conversation_id,
            mime_type=result.mime_type,
            model=result.model,
            revised_prompt=result.revised_prompt,
        )


async def _run_codex_ai_task_chat_log(
    *,
    hass: HomeAssistant,
    entry: ConfigEntry,
    auth_client: CodexAuthClient,
    tokens: CodexTokenSet,
    codex: CodexClient,
    chat_log: conversation.ChatLog,
    entity_id: str,
    model: str,
    prompt: str,
    reasoning_effort: str,
    reasoning_summary: str,
    text_verbosity: str,
) -> None:
    """Run Codex over an AI Task chat log with one auth refresh retry."""
    for _iteration in range(MAX_TOOL_ITERATIONS):
        try:
            await _stream_codex_turn_into_chat_log(
                chat_log=chat_log,
                codex=codex,
                entity_id=entity_id,
                model=model,
                instructions=_instructions_from_chat_log(chat_log, prompt),
                input_items=await _codex_input_from_chat_log(hass, chat_log),
                tools=_codex_tools_from_chat_log(chat_log),
                reasoning_effort=reasoning_effort,
                reasoning_summary=reasoning_summary,
                text_verbosity=text_verbosity,
            )
        except CodexAuthenticationError as err:
            LOGGER.warning(
                "Codex Assist AI Task access token was rejected; refreshing and retrying once: %s",
                err,
            )
            tokens = await _refresh_runtime_tokens(hass, entry, auth_client, tokens)
            codex = CodexClient(
                http_client=get_async_client(hass),
                access_token=tokens.access_token,
            )
            await _stream_codex_turn_into_chat_log(
                chat_log=chat_log,
                codex=codex,
                entity_id=entity_id,
                model=model,
                instructions=_instructions_from_chat_log(chat_log, prompt),
                input_items=await _codex_input_from_chat_log(hass, chat_log),
                tools=_codex_tools_from_chat_log(chat_log),
                reasoning_effort=reasoning_effort,
                reasoning_summary=reasoning_summary,
                text_verbosity=text_verbosity,
            )
        if not chat_log.unresponded_tool_results:
            break


async def _generate_codex_ai_task_image(
    *,
    hass: HomeAssistant,
    entry: ConfigEntry,
    auth_client: CodexAuthClient,
    tokens: CodexTokenSet,
    codex: CodexClient,
    chat_log: conversation.ChatLog,
    task: ai_task.GenImageTask,
    chat_model: str,
    image_model: str,
) -> CodexImageResult:
    """Run Codex image generation with one auth refresh retry."""
    try:
        return await codex.generate_image(
            prompt=task.instructions,
            input_items=await _codex_input_from_chat_log(hass, chat_log),
            chat_model=chat_model,
            image_model=image_model,
        )
    except CodexAuthenticationError as err:
        LOGGER.warning(
            "Codex Assist AI Task image access token was rejected; "
            "refreshing and retrying once: %s",
            err,
        )
        tokens = await _refresh_runtime_tokens(hass, entry, auth_client, tokens)
        codex = CodexClient(
            http_client=get_async_client(hass),
            access_token=tokens.access_token,
        )
        return await codex.generate_image(
            prompt=task.instructions,
            input_items=await _codex_input_from_chat_log(hass, chat_log),
            chat_model=chat_model,
            image_model=image_model,
        )


def _structured_data_from_text(text: str, structure: dict[str, Any] | None) -> Any:
    """Return plain text or parsed JSON for structured AI Task requests."""
    if not structure:
        return text
    try:
        return json_loads(text)
    except ValueError as err:
        LOGGER.error("Failed to parse Codex Assist AI Task JSON response: %s", text)
        raise HomeAssistantError("Codex Assist AI Task returned invalid JSON") from err
