from __future__ import annotations

import httpx
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client

from .codex_auth import CodexAuthClient
from .codex_client import CodexClient, CodexMessage
from .codex_runtime import resolve_runtime_tokens


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
    _attr_supported_features = conversation.ConversationEntityFeature(0)
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
        except RuntimeError:
            response.async_set_speech(
                "Codex Assist is missing Codex access token data. Reconfigure the integration."
            )
            return conversation.ConversationResult(
                response=response,
                conversation_id=user_input.conversation_id,
            )

        codex = CodexClient(http_client=http_client, access_token=tokens.access_token)
        try:
            text = await codex.generate_text(
                model=model,
                instructions=prompt,
                messages=_codex_messages_from_chat_log(chat_log, user_input),
            )
        except (httpx.HTTPError, RuntimeError) as err:
            text = f"Codex Assist failed: {err}"
        else:
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=user_input.agent_id,
                    content=text,
                )
            )

        response.async_set_speech(text or "Codex returned an empty response.")
        return conversation.ConversationResult(
            response=response,
            conversation_id=user_input.conversation_id,
        )


def _codex_messages_from_chat_log(
    chat_log: conversation.ChatLog,
    user_input: conversation.ConversationInput,
) -> list[CodexMessage]:
    messages: list[CodexMessage] = []
    for content in chat_log.content:
        role = getattr(content, "role", None)
        text = getattr(content, "content", None)
        if not isinstance(text, str) or not text.strip():
            continue
        if role == "user":
            messages.append(CodexMessage(role="user", content=text))
        elif role == "assistant":
            messages.append(CodexMessage(role="assistant", content=text))

    if not messages or messages[-1].role != "user" or messages[-1].content != user_input.text:
        messages.append(CodexMessage(role="user", content=user_input.text))

    return messages[-12:]
