"""Version 2 Codex Assist config entries for the real Home Assistant suite."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_assist import DOMAIN


def codex_entry(
    credentials: dict[str, str],
    *,
    conversation: dict[str, object] | None = None,
    ai_task: dict[str, object] | None = None,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Codex Assist",
        unique_id=DOMAIN,
        version=2,
        data=dict(credentials),
        subentries_data=[
            {
                "subentry_type": "conversation",
                "title": "Codex Assist",
                "data": {"model": "gpt-5.4", **(conversation or {})},
                "unique_id": None,
            },
            {
                "subentry_type": "ai_task_data",
                "title": "Codex Assist AI Task",
                "data": {"model": "gpt-5.4", **(ai_task or {})},
                "unique_id": None,
            },
        ],
    )


def subentry_of(entry: MockConfigEntry, subentry_type: str):
    (found,) = (s for s in entry.subentries.values() if s.subentry_type == subentry_type)
    return found


async def update_settings(
    hass: HomeAssistant, entry: MockConfigEntry, subentry_type: str, **settings: object
) -> None:
    """Change one agent's settings and wait for the reload that applies them."""
    target = subentry_of(entry, subentry_type)
    hass.config_entries.async_update_subentry(entry, target, data={**target.data, **settings})
    await hass.async_block_till_done()
