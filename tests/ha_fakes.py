from __future__ import annotations

import sys
import types
from dataclasses import dataclass


def install_homeassistant_fakes(monkeypatch):
    """Install minimal Home Assistant modules needed for behavior-unit tests."""

    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    conversation = types.ModuleType("homeassistant.components.conversation")
    ai_task = types.ModuleType("homeassistant.components.ai_task")
    config_entries = types.ModuleType("homeassistant.config_entries")
    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    exceptions = types.ModuleType("homeassistant.exceptions")
    helpers = types.ModuleType("homeassistant.helpers")
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    httpx_client = types.ModuleType("homeassistant.helpers.httpx_client")
    intent = types.ModuleType("homeassistant.helpers.intent")
    llm = types.ModuleType("homeassistant.helpers.llm")
    selector = types.ModuleType("homeassistant.helpers.selector")
    util = types.ModuleType("homeassistant.util")
    util_json = types.ModuleType("homeassistant.util.json")
    voluptuous_openapi = types.ModuleType("voluptuous_openapi")
    vol = types.ModuleType("voluptuous")

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        async def async_set_unique_id(self, unique_id):
            self.unique_id = unique_id

        def _abort_if_unique_id_configured(self):
            self.duplicate_checked = True

        def async_update_reload_and_abort(self, entry, data_updates):
            reason = (
                "reconfigure_successful"
                if getattr(self, "source", "") == "reconfigure"
                else "reauth_successful"
            )
            return {
                "type": "abort",
                "reason": reason,
                "entry": entry,
                "data_updates": data_updates,
            }

        def _get_reauth_entry(self):
            return getattr(self, "reauth_entry", object())

        def _get_reconfigure_entry(self):
            return getattr(self, "reconfigure_entry", object())

    class OptionsFlow:
        @property
        def config_entry(self):
            return self._config_entry

        @config_entry.setter
        def config_entry(self, value):
            self._config_entry = value

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

    class ConfigEntry:
        pass

    class HomeAssistant:
        pass

    class ConversationEntity:
        pass

    class AbstractConversationAgent:
        pass

    class ConversationEntityFeature:
        CONTROL = 1

    @dataclass
    class AssistantContent:
        agent_id: str | None
        content: str | None = None
        tool_calls: list | None = None

    @dataclass
    class ConversationResult:
        response: object
        conversation_id: str | None = None

    @dataclass
    class UserContent:
        content: str
        attachments: list | None = None
        role: str = "user"

    @dataclass
    class ToolInput:
        id: str
        tool_name: str
        tool_args: dict

    class IntentResponse:
        def __init__(self, language=None):
            self.language = language
            self.speech = None

        def async_set_speech(self, speech):
            self.speech = speech

    class SelectSelectorMode:
        DROPDOWN = "dropdown"

    @dataclass
    class SelectOptionDict:
        value: str
        label: str

    @dataclass
    class SelectSelectorConfig:
        options: list
        mode: str

    @dataclass
    class SelectSelector:
        config: SelectSelectorConfig

    class Section:
        def __init__(self, schema, options=None):
            self.schema = schema
            self.options = {"collapsed": False, **(options or {})}

        def __call__(self, value):
            return self.schema(value)

    class Schema:
        def __init__(self, schema):
            self.schema = schema

        def __call__(self, value):
            return value

    class Invalid(Exception):
        pass

    class Optional:
        def __init__(self, key, default=None):
            self.key = key
            self.default = default

        def __hash__(self):
            return hash((self.key, self.default))

        def __eq__(self, other):
            return (
                isinstance(other, Optional)
                and self.key == other.key
                and self.default == other.default
            )

    class Required(Optional):
        pass

    config_entries.ConfigFlow = ConfigFlow
    config_entries.ConfigEntry = ConfigEntry
    config_entries.OptionsFlow = OptionsFlow
    config_entries.SOURCE_REAUTH = "reauth"
    config_entries.SOURCE_RECONFIGURE = "reconfigure"
    data_entry_flow.section = Section  # type: ignore[attr-defined]
    conversation.AbstractConversationAgent = AbstractConversationAgent
    conversation.AssistantContent = AssistantContent
    conversation.AssistantContentDeltaDict = dict
    conversation.ConversationInput = object
    conversation.ConversationResult = ConversationResult
    conversation.ConversationEntity = ConversationEntity
    conversation.ConversationEntityFeature = ConversationEntityFeature
    conversation.UserContent = UserContent
    conversation.async_set_agent = lambda hass, entry, agent: None
    conversation.async_unset_agent = lambda hass, entry: None
    conversation.async_get_result_from_chat_log = lambda user_input, chat_log: (
        user_input,
        chat_log,
    )
    conversation.ConverseError = RuntimeError
    ai_task.AITaskEntity = type("AITaskEntity", (), {})
    ai_task.AITaskEntityFeature = types.SimpleNamespace(
        GENERATE_DATA=1,
        GENERATE_IMAGE=2,
        SUPPORT_ATTACHMENTS=4,
    )
    ai_task.GenDataTaskResult = object
    ai_task.GenImageTaskResult = object
    const.MATCH_ALL = "*"
    core.HomeAssistant = HomeAssistant
    exceptions.HomeAssistantError = RuntimeError
    entity_platform.AddConfigEntryEntitiesCallback = object
    httpx_client.get_async_client = lambda hass: getattr(hass, "http_client", None)
    intent.IntentResponse = IntentResponse
    llm.LLM_API_ASSIST = "assist"
    llm.ToolInput = ToolInput
    selector.SelectOptionDict = SelectOptionDict
    selector.SelectSelector = SelectSelector
    selector.SelectSelectorConfig = SelectSelectorConfig
    selector.SelectSelectorMode = SelectSelectorMode
    util_json.json_loads = __import__("json").loads
    util.slugify = lambda value: value.lower().replace(" ", "_")
    voluptuous_openapi.convert = lambda schema, custom_serializer=None: getattr(
        schema, "schema", schema
    )
    vol.Invalid = Invalid
    vol.Schema = Schema
    vol.Optional = Optional
    vol.Required = Required  # type: ignore[attr-defined]

    ha.components = components
    ha.config_entries = config_entries
    ha.data_entry_flow = data_entry_flow  # type: ignore[attr-defined]
    components.conversation = conversation
    components.ai_task = ai_task
    helpers.entity_platform = entity_platform
    helpers.httpx_client = httpx_client
    helpers.intent = intent
    helpers.llm = llm
    helpers.selector = selector
    util.json = util_json

    modules = {
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.ai_task": ai_task,
        "homeassistant.components.conversation": conversation,
        "homeassistant.config_entries": config_entries,
        "homeassistant.data_entry_flow": data_entry_flow,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_platform": entity_platform,
        "homeassistant.helpers.httpx_client": httpx_client,
        "homeassistant.helpers.intent": intent,
        "homeassistant.helpers.llm": llm,
        "homeassistant.helpers.selector": selector,
        "homeassistant.util": util,
        "homeassistant.util.json": util_json,
        "voluptuous": vol,
        "voluptuous_openapi": voluptuous_openapi,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
