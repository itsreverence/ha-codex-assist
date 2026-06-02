from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.httpx_client import get_async_client

from . import DOMAIN
from .codex_auth import (
    CODEX_DEVICE_VERIFICATION_URL,
    CodexAuthClient,
    CodexDeviceCode,
)

CONF_ACCESS_TOKEN = "access_token"
CONF_PROMPT = "prompt"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_SAFETY_MODE = "safety_mode"
CONF_MODEL = "model"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_PROMPT = "You are a concise Home Assistant Assist conversation agent."
SAFETY_MODE_TALK_ONLY = "talk_only"
DEFAULT_SAFETY_MODE = SAFETY_MODE_TALK_ONLY


class CodexAssistConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> CodexAssistOptionsFlow:
        return CodexAssistOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._setup_input: dict[str, Any] = {}
        self._device_code: CodexDeviceCode | None = None

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
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
        return self.async_create_entry(title="Codex Assist", data=data)

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
    return _settings_schema({})


class CodexAssistOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(user_input))

        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(defaults),
        )


def _settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_MODEL, default=defaults.get(CONF_MODEL, DEFAULT_MODEL)): str,
            vol.Optional(
                CONF_PROMPT,
                default=defaults.get(CONF_PROMPT, DEFAULT_PROMPT),
            ): str,
            vol.Optional(
                CONF_SAFETY_MODE,
                default=defaults.get(CONF_SAFETY_MODE, DEFAULT_SAFETY_MODE),
            ): vol.In([SAFETY_MODE_TALK_ONLY]),
        }
    )
