# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Shared base for Vigil's coordinator-backed entities.

Centralizes the identity wiring every Vigil entity repeats: the has-entity-name
flag, the ``<entry_id>_<suffix>`` unique id, and the shared device grouping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import vigil_device_info

if TYPE_CHECKING:
    from .coordinator import VigilCoordinator


class VigilEntity(CoordinatorEntity["VigilCoordinator"]):
    """Base for every Vigil entity: shared name/unique-id/device wiring.

    Subclasses mix this with the platform entity (``SensorEntity`` /
    ``ButtonEntity``) and pass their own unique-id suffix.
    """

    _attr_has_entity_name = True

    def __init__(
        self, entry_id: str, coordinator: VigilCoordinator, suffix: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{suffix}"
        self._attr_device_info = vigil_device_info(entry_id)
