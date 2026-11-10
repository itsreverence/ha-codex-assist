from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.httpx_client import get_async_client

from . import DOMAIN
from .codex_auth import (
    CODEX_DEVICE_VERIFICATION_URL,
    CodexAuthClient,
    CodexDeviceCode,
)
from .codex_image import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_IMAGE_SIZE,
    IMAGE_MODEL_QUALITY,
    IMAGE_SIZE_OPTIONS,
)
from .codex_models import ModelCatalog, ModelDiscoveryCache, fetch_codex_model_ids
from .codex_runtime import runtime_token_coordinator
from .model_discovery import async_entry_model_catalog

CONF_ACCESS_TOKEN = "access_token"
CONF_NAME = "name"
CONF_PROMPT = "prompt"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_MODEL = "model"
CONF_IMAGE_MODEL = "image_model"
CONF_IMAGE_SIZE = "image_size"
CONF_REASONING_EFFORT = "reasoning_effort"
CONF_REASONING_SUMMARY = "reasoning_summary"
CONF_TEXT_VERBOSITY = "text_verbosity"
CONF_WEB_SEARCH = "web_search"
SECTION_CHAT_SETTINGS = "chat_settings"
SECTION_ADVANCED_SETTINGS = "advanced_settings"
SECTION_IMAGE_SETTINGS = "image_settings"
DEFAULT_PROMPT = "You are a concise Home Assistant Assist conversation agent."
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_REASONING_SUMMARY = "auto"
DEFAULT_TEXT_VERBOSITY = "medium"
DEFAULT_WEB_SEARCH = False

CREDENTIAL_KEYS = (CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN)
SUBENTRY_CONVERSATION = "conversation"
SUBENTRY_AI_TASK = "ai_task_data"
SUBENTRY_TITLES = {
    SUBENTRY_CONVERSATION: "Codex Assist",
    SUBENTRY_AI_TASK: "Codex Assist AI Task",
}
SUBENTRY_SETTING_KEYS = {
    SUBENTRY_CONVERSATION: frozenset(
        {
            CONF_MODEL,
            CONF_PROMPT,
            CONF_REASONING_EFFORT,
            CONF_REASONING_SUMMARY,
            CONF_TEXT_VERBOSITY,
            CONF_WEB_SEARCH,
        }
    ),
    SUBENTRY_AI_TASK: frozenset(
        {
            CONF_MODEL,
            CONF_REASONING_EFFORT,
            CONF_REASONING_SUMMARY,
            CONF_TEXT_VERBOSITY,
            CONF_WEB_SEARCH,
            CONF_IMAGE_MODEL,
            CONF_IMAGE_SIZE,
        }
    ),
}
RECOMMENDED_SETTINGS = {
    CONF_PROMPT: DEFAULT_PROMPT,
    CONF_REASONING_EFFORT: DEFAULT_REASONING_EFFORT,
    CONF_TEXT_VERBOSITY: DEFAULT_TEXT_VERBOSITY,
    CONF_WEB_SEARCH: DEFAULT_WEB_SEARCH,
    CONF_IMAGE_MODEL: DEFAULT_IMAGE_MODEL,
    CONF_IMAGE_SIZE: DEFAULT_IMAGE_SIZE,
}


def subentry_settings(subentry_type: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    keys = SUBENTRY_SETTING_KEYS[subentry_type]
    return {key: value for key, value in settings.items() if key in keys}


class CodexAssistConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        del config_entry
        return {
            SUBENTRY_CONVERSATION: CodexAssistSubentryFlow,
            SUBENTRY_AI_TASK: CodexAssistSubentryFlow,
        }

    def __init__(self) -> None:
        self._device_code: CodexDeviceCode | None = None
        self._credentials: dict[str, Any] = {}
        self._model_cache = ModelDiscoveryCache()
        self._catalog: ModelCatalog | None = None

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            try:
                self._device_code = await self._auth_client().request_device_code()
            except RuntimeError:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_user_schema(),
                    errors={"base": "device_code_request_failed"},
                )
            return await self.async_step_device()

        return self.async_show_form(step_id="user", data_schema=_user_schema())

    async def async_step_device(self, user_input=None):
        if user_input is not None:
            return await self.async_step_device_wait()
        return self._show_device_form()

    async def async_step_device_wait(self, user_input=None):
        del user_input
        if self._device_code is None:
            return await self.async_step_user()

        auth_client = self._auth_client()
        try:
            authorization = await auth_client.poll_device_code(
                device_auth_id=self._device_code.device_auth_id,
                user_code=self._device_code.user_code,
            )
            if authorization is None:
                return self._show_device_form(errors={"base": "authorization_pending"})
            tokens = await auth_client.exchange_authorization_code(authorization)
        except RuntimeError:
            return self._show_device_form(errors={"base": "device_code_auth_failed"})

        credentials = {
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
        }
        if self.source in (config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE):
            entry = (
                self._get_reauth_entry()
                if self.source == config_entries.SOURCE_REAUTH
                else self._get_reconfigure_entry()
            )
            _clear_model_cache(entry)
            # Entities read credentials at request time, so no reload is needed.
            return self.async_update_and_abort(entry, data_updates=credentials)

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self._credentials = credentials
        return await self.async_step_model()

    async def async_step_model(self, user_input=None):
        errors = {}
        if user_input is not None and self._catalog and self._catalog.models:
            model = user_input.get(CONF_MODEL)
            if model in self._catalog.models:
                settings = {**RECOMMENDED_SETTINGS, CONF_MODEL: model}
                return self.async_create_entry(
                    title="Codex Assist",
                    data=self._credentials,
                    subentries=[
                        {
                            "subentry_type": subentry_type,
                            "data": subentry_settings(subentry_type, settings),
                            "title": title,
                            "unique_id": None,
                        }
                        for subentry_type, title in SUBENTRY_TITLES.items()
                    ],
                )
            errors["base"] = "invalid_model"
        if self._catalog is None or not self._catalog.models:
            self._catalog = await self._model_cache.async_get(
                lambda: fetch_codex_model_ids(
                    http_client=get_async_client(self.hass),
                    access_token=self._credentials.get(CONF_ACCESS_TOKEN),
                ),
                force=True,
            )
        if not self._catalog.models:
            errors["base"] = "no_models"
        return self.async_show_form(
            step_id="model",
            data_schema=_model_schema({}, model_options=list(self._catalog.models)),
            errors=errors,
            description_placeholders={"model_status": _catalog_description(self._catalog)},
        )

    async def async_step_reconfigure(self, user_input=None):
        if user_input is not None:
            try:
                self._device_code = await self._auth_client().request_device_code()
            except RuntimeError:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=vol.Schema({}),
                    errors={"base": "device_code_request_failed"},
                )
            return await self.async_step_device()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({}),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        del entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        try:
            self._device_code = await self._auth_client().request_device_code()
        except RuntimeError:
            return self.async_show_form(
                step_id="reauth_confirm",
                errors={"base": "device_code_request_failed"},
            )
        return await self.async_step_device()

    def _auth_client(self) -> CodexAuthClient:
        return CodexAuthClient(http_client=get_async_client(self.hass))

    def _show_device_form(self, errors=None):
        device_code = self._device_code
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "verification_uri": CODEX_DEVICE_VERIFICATION_URL,
                "user_code": device_code.user_code if device_code else "",
                "interval": str(device_code.interval) if device_code else "5",
            },
        )


def _user_schema() -> vol.Schema:
    return vol.Schema({})


def _clear_model_cache(entry) -> None:
    runtime_token_coordinator(entry).model_cache.clear()


class CodexAssistSubentryFlow(config_entries.ConfigSubentryFlow):
    def __init__(self) -> None:
        self._catalog: ModelCatalog | None = None

    async def async_step_user(self, user_input=None):
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self.async_step_init(user_input)

    async def async_step_init(self, user_input=None):
        entry = self._get_entry()
        if entry.state is not config_entries.ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        is_new = self.source == config_entries.SOURCE_USER
        saved = {} if is_new else dict(self._get_reconfigure_subentry().data)
        saved_model = saved.get(CONF_MODEL)
        if self._catalog is None:
            self._catalog = await async_entry_model_catalog(self.hass, entry, force=True)

        errors = {}
        if user_input is not None:
            data = subentry_settings(
                self._subentry_type, {**saved, **_flatten_settings_input(user_input)}
            )
            model = data.get(CONF_MODEL)
            if model is None:
                errors["base"] = "no_models"
            elif model not in self._catalog.models and model != saved_model:
                errors["base"] = "invalid_model"
            elif is_new:
                return self.async_create_entry(title=user_input[CONF_NAME], data=data)
            else:
                return self.async_update_and_abort(
                    entry, self._get_reconfigure_subentry(), data=data
                )

        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(
                saved,
                model_options=list(self._catalog.models),
                keys=SUBENTRY_SETTING_KEYS[self._subentry_type],
                name_default=SUBENTRY_TITLES[self._subentry_type] if is_new else None,
            ),
            errors=errors,
            description_placeholders={
                "model_status": _catalog_description(self._catalog, saved_model)
            },
        )


def _settings_schema(
    defaults: Mapping[str, Any],
    *,
    model_options: list[str],
    keys: frozenset[str],
    name_default: str | None = None,
) -> vol.Schema:
    model_options = list(dict.fromkeys(model_options))
    saved_model = defaults.get(CONF_MODEL)
    model_default = saved_model or next(iter(model_options), None)
    image_model_default = defaults.get(CONF_IMAGE_MODEL, DEFAULT_IMAGE_MODEL)
    if image_model_default not in IMAGE_MODEL_QUALITY:
        image_model_default = DEFAULT_IMAGE_MODEL
    image_size_default = defaults.get(CONF_IMAGE_SIZE, DEFAULT_IMAGE_SIZE)
    if image_size_default not in IMAGE_SIZE_OPTIONS:
        image_size_default = DEFAULT_IMAGE_SIZE

    def field(key: str, default: Any, validator: Any) -> dict[Any, Any]:
        if key not in keys or default is None:
            return {}
        return {vol.Optional(key, default=default): validator}

    sections = (
        (
            SECTION_CHAT_SETTINGS,
            False,
            {
                **field(
                    CONF_MODEL,
                    model_default,
                    _model_selector(model_options, saved_model=saved_model),
                ),
                **field(
                    CONF_TEXT_VERBOSITY,
                    defaults.get(CONF_TEXT_VERBOSITY, DEFAULT_TEXT_VERBOSITY),
                    _low_medium_high_selector(),
                ),
                **field(
                    CONF_WEB_SEARCH, defaults.get(CONF_WEB_SEARCH, DEFAULT_WEB_SEARCH), bool
                ),
            },
        ),
        (
            SECTION_ADVANCED_SETTINGS,
            True,
            {
                **field(
                    CONF_PROMPT,
                    defaults.get(CONF_PROMPT, DEFAULT_PROMPT),
                    selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
                ),
                **field(
                    CONF_REASONING_EFFORT,
                    defaults.get(CONF_REASONING_EFFORT, DEFAULT_REASONING_EFFORT),
                    _low_medium_high_selector(),
                ),
            },
        ),
        (
            SECTION_IMAGE_SETTINGS,
            True,
            {
                **field(CONF_IMAGE_MODEL, image_model_default, _image_model_selector()),
                **field(CONF_IMAGE_SIZE, image_size_default, _image_size_selector()),
            },
        ),
    )
    schema: dict[Any, Any] = {}
    if name_default is not None:
        schema[vol.Required(CONF_NAME, default=name_default)] = str
    for section_name, collapsed, fields in sections:
        if fields:
            schema[vol.Required(section_name)] = section(
                vol.Schema(fields), {"collapsed": collapsed}
            )
    return vol.Schema(schema)


def _model_schema(defaults: dict[str, Any], *, model_options: list[str]) -> vol.Schema:
    model_options = list(dict.fromkeys(model_options))
    saved_model = defaults.get(CONF_MODEL)
    model_default = (
        saved_model
        if saved_model in model_options
        else next(iter(model_options), None)
    )
    if not model_options:
        return vol.Schema({})
    return vol.Schema(
        {vol.Optional(CONF_MODEL, default=model_default): _model_selector(model_options)}
    )


def _flatten_settings_input(user_input: Mapping[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for section_name in (
        SECTION_CHAT_SETTINGS,
        SECTION_ADVANCED_SETTINGS,
        SECTION_IMAGE_SETTINGS,
    ):
        section_data = user_input.get(section_name)
        if isinstance(section_data, Mapping):
            settings.update(section_data)
    return settings


def _low_medium_high_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["low", "medium", "high"],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _model_selector(
    model_options: list[str], *, saved_model: str | None = None
) -> selector.SelectSelector:
    options = [selector.SelectOptionDict(value=model, label=model) for model in model_options]
    if saved_model and saved_model not in model_options:
        options.append(
            selector.SelectOptionDict(
                value=saved_model, label=f"{saved_model} (saved; not currently listed)"
            )
        )
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _image_model_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="gpt-image-2-low", label="Low"),
                selector.SelectOptionDict(value="gpt-image-2-medium", label="Medium"),
                selector.SelectOptionDict(value="gpt-image-2-high", label="High"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _image_size_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="1024x1024", label="Square (1024×1024)"),
                selector.SelectOptionDict(value="1536x1024", label="Landscape (1536×1024)"),
                selector.SelectOptionDict(value="1024x1536", label="Portrait (1024×1536)"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _catalog_description(catalog: ModelCatalog, saved_model: str | None = None) -> str:
    if catalog.source == "fallback":
        status = "Discovery is unavailable. Suggested models are unverified for this account."
    elif not catalog.models:
        status = "The account's model list contains no visible models."
    elif catalog.source == "cached":
        status = "Showing the last model list retrieved for this account during this session."
    else:
        status = "Models retrieved from your ChatGPT/Codex account."
    if catalog.error == "authentication":
        status += " Account discovery requires a fresh sign-in."
    elif catalog.error and catalog.source == "cached":
        status += " Refresh failed; the cached list may be outdated."
    if saved_model and saved_model not in catalog.models:
        status += (
            " Your saved model is not in this list; it is kept until you choose a replacement."
        )
    return status
