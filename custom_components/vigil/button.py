# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Button platform — a "Clear acknowledgements" action for the Vigil device.

Pressing it forgets every dismissed-alert acknowledgement, so all currently
active issues re-surface in the notification on the next (immediate) cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VigilConfigEntry
from .entity import VigilEntity

if TYPE_CHECKING:
    from .coordinator import VigilCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VigilConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Vigil action buttons."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([VigilClearAcknowledgementsButton(entry.entry_id, coordinator)])


class VigilClearAcknowledgementsButton(VigilEntity, ButtonEntity):
    """Forget all dismissed-alert acknowledgements so every issue re-alerts."""

    _attr_translation_key = "clear_acknowledgements"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry_id: str, coordinator: VigilCoordinator) -> None:
        super().__init__(entry_id, coordinator, "clear_acknowledgements")

    @property
    def available(self) -> bool:
        """Always pressable: clearing acknowledgements is stateless, so a failed
        update cycle (which would disable a plain CoordinatorEntity) must not
        disable it — that's exactly when a user may want to reset state."""
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_clear_acknowledgements()
