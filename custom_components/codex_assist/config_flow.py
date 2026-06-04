from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.httpx_client import get_async_client

from . import DOMAIN
from .codex_auth import (
    CODEX_DEVICE_VERIFICATION_URL,
    CodexAuthClient,
    CodexDeviceCode,
)
from .codex_models import DEFAULT_CODEX_MODELS, fetch_codex_model_ids

CONF_ACCESS_TOKEN = "access_token"
CONF_PROMPT = "prompt"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_MODEL = "model"
CONF_REASONING_EFFORT = "reasoning_effort"
CONF_REASONING_SUMMARY = "reasoning_summary"
CONF_TEXT_VERBOSITY = "text_verbosity"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_PROMPT = "You are a concise Home Assistant Assist conversation agent."
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_REASONING_SUMMARY = "auto"
DEFAULT_TEXT_VERBOSITY = "medium"


class CodexAssistConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> CodexAssistOptionsFlow:
        del config_entry
        return CodexAssistOptionsFlow()

    def __init__(self) -> None:
        self._setup_input: dict[str, Any] = {}
        self._device_code: CodexDeviceCode | None = None

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self._setup_input = dict(user_input)
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

        data = {
            **self._setup_input,
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
        }
        if self.source == config_entries.SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates=data,
            )

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Codex Assist", data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        self._setup_input = {
            key: entry_data[key]
            for key in (
                CONF_MODEL,
                CONF_PROMPT,
                CONF_REASONING_EFFORT,
                CONF_REASONING_SUMMARY,
                CONF_TEXT_VERBOSITY,
            )
            if key in entry_data
        }
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
    return _settings_schema({}, model_options=DEFAULT_CODEX_MODELS)


class CodexAssistOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(user_input))

        defaults = {**self.config_entry.data, **self.config_entry.options}
        model_options = await fetch_codex_model_ids(
            http_client=get_async_client(self.hass),
            access_token=self.config_entry.data.get(CONF_ACCESS_TOKEN),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(defaults, model_options=model_options),
        )


def _settings_schema(
    defaults: dict[str, Any],
    *,
    model_options: list[str],
) -> vol.Schema:
    model_default = defaults.get(CONF_MODEL, DEFAULT_MODEL)
    model_options = list(dict.fromkeys([*model_options, str(model_default), DEFAULT_MODEL]))

    return vol.Schema(
        {
            vol.Optional(CONF_MODEL, default=model_default): _model_selector(model_options),
            vol.Optional(
                CONF_PROMPT,
                default=defaults.get(CONF_PROMPT, DEFAULT_PROMPT),
            ): str,
            vol.Optional(
                CONF_REASONING_EFFORT,
                default=defaults.get(CONF_REASONING_EFFORT, DEFAULT_REASONING_EFFORT),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["low", "medium", "high"],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_REASONING_SUMMARY,
                default=defaults.get(CONF_REASONING_SUMMARY, DEFAULT_REASONING_SUMMARY),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["auto", "concise", "detailed", "off"],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_TEXT_VERBOSITY,
                default=defaults.get(CONF_TEXT_VERBOSITY, DEFAULT_TEXT_VERBOSITY),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["low", "medium", "high"],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _model_selector(model_options: list[str]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=model, label=model) for model in model_options
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
            custom_value=True,
        )
    )
