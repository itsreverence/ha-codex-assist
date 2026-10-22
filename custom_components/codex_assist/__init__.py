from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

DOMAIN = "codex_assist"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from datetime import timedelta

    from homeassistant.const import Platform
    from homeassistant.helpers.event import async_track_time_interval

    from .codex_models import MODEL_CACHE_SECONDS
    from .model_discovery import async_entry_model_catalog

    await hass.config_entries.async_forward_entry_setups(
        entry,
        (Platform.CONVERSATION, Platform.AI_TASK),
    )

    async def refresh_models(_now) -> None:
        await async_entry_model_catalog(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(hass, refresh_models, timedelta(seconds=MODEL_CACHE_SECONDS))
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.const import Platform

    return await hass.config_entries.async_unload_platforms(
        entry,
        (Platform.CONVERSATION, Platform.AI_TASK),
    )
