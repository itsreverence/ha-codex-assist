from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

DOMAIN = "codex_assist"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.const import Platform

    await hass.config_entries.async_forward_entry_setups(
        entry,
        (Platform.CONVERSATION, Platform.AI_TASK),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.const import Platform

    return await hass.config_entries.async_unload_platforms(
        entry,
        (Platform.CONVERSATION, Platform.AI_TASK),
    )
