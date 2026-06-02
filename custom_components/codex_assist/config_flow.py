from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries

from . import DOMAIN


class CodexAssistConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Codex Assist", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional("model", default="gpt-5.4"): str,
                vol.Optional(
                    "prompt",
                    default="You are a concise Home Assistant Assist conversation agent.",
                ): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
