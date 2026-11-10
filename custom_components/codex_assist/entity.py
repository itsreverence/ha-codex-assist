from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from . import DOMAIN
from .config_flow import CONF_MODEL

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, ConfigSubentry


class CodexAssistSubentryEntity(Entity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="OpenAI",
            model=subentry.data.get(CONF_MODEL),
            entry_type=DeviceEntryType.SERVICE,
        )
