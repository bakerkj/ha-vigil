# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VigilConfigEntry
from .entity import VigilEntity
from .models import ISSUE_BUCKETS

if TYPE_CHECKING:
    from .coordinator import VigilCoordinator


@dataclass(frozen=True, kw_only=True)
class VigilSensorDescription(SensorEntityDescription):
    """Describes a Vigil count sensor."""

    count_key: str


# Lean count sensors, recorded (state_class=measurement); the full per-issue
# detail lives on sensor.vigil_state to stay under the recorder's 16 KB limit.
SENSOR_DESCRIPTIONS: tuple[VigilSensorDescription, ...] = (
    VigilSensorDescription(
        key="total_issues",
        count_key="total",
        translation_key="total_issues",
        icon="mdi:shield-alert",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="issues",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VigilSensorDescription(
        key="integration_failures",
        count_key="integration_failures",
        translation_key="integration_failures",
        icon="mdi:puzzle-remove",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="issues",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VigilSensorDescription(
        key="devices_offline",
        count_key="devices_offline",
        translation_key="devices_offline",
        icon="mdi:lan-disconnect",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="issues",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VigilSensorDescription(
        key="stale_devices",
        count_key="stale_devices",
        translation_key="stale_devices",
        icon="mdi:timer-sand",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="issues",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VigilSensorDescription(
        key="device_faults",
        count_key="device_faults",
        translation_key="device_faults",
        icon="mdi:alert-circle-check",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="issues",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VigilSensorDescription(
        key="app_issues",
        count_key="app_issues",
        translation_key="app_issues",
        icon="mdi:puzzle-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="issues",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VigilConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Vigil count sensors plus the detail (state) sensor."""
    entry_data = entry.runtime_data
    entities: list[SensorEntity] = [
        VigilSensor(entry.entry_id, entry_data.coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(VigilStateSensor(entry.entry_id, entry_data.coordinator))
    async_add_entities(entities)


class VigilSensor(VigilEntity, SensorEntity):
    """A small, recorded diagnostic sensor exposing one Vigil issue count."""

    entity_description: VigilSensorDescription

    def __init__(
        self,
        entry_id: str,
        coordinator: VigilCoordinator,
        description: VigilSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(entry_id, coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        """Return the current count for this sensor."""
        data = self.coordinator.data
        if not data:
            return None
        return data["counts"].get(self.entity_description.count_key, 0)


class VigilStateSensor(VigilEntity, SensorEntity):
    """Compact status sensor (ok / issues / starting) with a small summary.

    The full per-issue payload is served by the HTTP API / card feed
    (``/api/vigil/state``), not this sensor's attributes, to stay under the
    recorder's 16 KB per-attribute limit.
    """

    _attr_translation_key = "state"
    _attr_icon = "mdi:shield-search"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry_id: str, coordinator: VigilCoordinator) -> None:
        """Initialize the detail sensor."""
        super().__init__(entry_id, coordinator, "state")

    @property
    def native_value(self) -> str | None:
        """Compact status string (the heavy detail is in attributes)."""
        data = self.coordinator.data
        if not data:
            return None
        if data["startup_grace_active"]:
            return "starting"
        return "ok" if data["healthy"] else "issues"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Small summary only (counts + health); the full payload is on the HTTP
        API / card feed to stay under the recorder's 16 KB limit."""
        data = self.coordinator.data
        if not data:
            return None
        last_run = data.get("last_run")
        counts = data["counts"]
        attrs: dict[str, Any] = {
            "healthy": data["healthy"],
            "startup_grace_active": data["startup_grace_active"],
            "last_run": last_run.isoformat() if last_run else None,
            "total_issues": counts["total"],
        }
        # One attribute per canonical bucket so the sensor tracks the counts.
        for bucket in ISSUE_BUCKETS:
            attrs[bucket.key] = counts.get(bucket.key, 0)
        return attrs
