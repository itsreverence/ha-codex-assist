"""Diagnostics support for Codex Assist."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

REDACTED = "**REDACTED**"
TO_REDACT = {"access_token", "refresh_token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a Codex Assist config entry."""
    del hass
    return {
        "entry": {
            "data": redact_sensitive(dict(entry.data)),
            "options": redact_sensitive(dict(entry.options)),
        },
        "subentries": [
            {
                "subentry_type": subentry.subentry_type,
                "title": subentry.title,
                "data": redact_sensitive(dict(subentry.data)),
            }
            for subentry in entry.subentries.values()
        ],
    }


def redact_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """Replace credential values with a redaction marker, keeping the keys."""
    return {key: REDACTED if key in TO_REDACT else value for key, value in data.items()}
