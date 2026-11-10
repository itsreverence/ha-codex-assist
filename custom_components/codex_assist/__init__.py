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
    from .codex_runtime import runtime_token_coordinator
    from .model_discovery import async_entry_model_catalog

    runtime_token_coordinator(entry)
    _adopt_v1_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(
        entry,
        (Platform.CONVERSATION, Platform.AI_TASK),
    )

    async def refresh_models(_now) -> None:
        await async_entry_model_catalog(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(hass, refresh_models, timedelta(seconds=MODEL_CACHE_SECONDS))
    )

    loaded_subentries = _subentry_snapshot(entry)

    async def reload_on_subentry_change(hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Token rotation rewrites entry.data on every refresh; only agent changes reload.
        if _subentry_snapshot(entry) != loaded_subentries:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(reload_on_subentry_change))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.const import Platform

    return await hass.config_entries.async_unload_platforms(
        entry,
        (Platform.CONVERSATION, Platform.AI_TASK),
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version > 2:
        return False
    if entry.version == 1:
        _migrate_settings_to_subentries(hass, entry)
    return True


def _subentry_snapshot(entry: ConfigEntry) -> dict[str, tuple[str, str, dict]]:
    # Subentry updates mutate the subentry in place, so copy what we compare.
    return {
        subentry_id: (subentry.subentry_type, subentry.title, dict(subentry.data))
        for subentry_id, subentry in entry.subentries.items()
    }


def _migrate_settings_to_subentries(hass: HomeAssistant, entry: ConfigEntry) -> None:
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigSubentry

    from .config_flow import CREDENTIAL_KEYS, SUBENTRY_TITLES, subentry_settings

    settings = {
        key: value
        for key, value in {**entry.data, **entry.options}.items()
        if key not in CREDENTIAL_KEYS
    }
    existing_types = {subentry.subentry_type for subentry in entry.subentries.values()}
    for subentry_type, title in SUBENTRY_TITLES.items():
        if subentry_type in existing_types:
            continue
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(subentry_settings(subentry_type, settings)),
                subentry_type=subentry_type,
                title=title,
                unique_id=None,
            ),
        )

    hass.config_entries.async_update_entry(
        entry,
        data={key: entry.data[key] for key in CREDENTIAL_KEYS if key in entry.data},
        options={},
        version=2,
    )


def _adopt_v1_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Runs on every setup: the entity registry saves later than config entries, so a
    # restart can see a v2 entry next to registry rows that still carry v1 unique IDs.
    from homeassistant.helpers import entity_registry as er

    from .config_flow import SUBENTRY_AI_TASK, SUBENTRY_CONVERSATION

    entity_registry = er.async_get(hass)
    for subentry_type, platform, v1_unique_id in (
        (SUBENTRY_CONVERSATION, "conversation", entry.entry_id),
        (SUBENTRY_AI_TASK, "ai_task", f"{entry.entry_id}_ai_task"),
    ):
        entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, v1_unique_id)
        subentry = next(
            (s for s in entry.subentries.values() if s.subentry_type == subentry_type), None
        )
        if entity_id is None or subentry is None:
            continue
        if entity_registry.async_get_entity_id(platform, DOMAIN, subentry.subentry_id):
            continue
        entity_registry.async_update_entity(
            entity_id,
            config_subentry_id=subentry.subentry_id,
            new_unique_id=subentry.subentry_id,
        )
