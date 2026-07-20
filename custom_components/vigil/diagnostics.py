# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Diagnostics for Vigil — a health monitor should be introspectable itself."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import VigilConfigEntry
from .const import (
    CONF_EXCLUDED_DEVICE_IDS,
    CONF_EXCLUDED_ENTITY_IDS,
    CONF_INTERVAL_STORE_URL,
    CONF_STALENESS_EXCLUDED_DEVICE_IDS,
    merged_options,
)
from .reporting.serialize import serialize_vigil_data

# Redact what leaks the setup when diagnostics are shared: exclusion lists carry
# entity/device ids, and the interval-store URL embeds DB credentials.
_REDACT = {
    CONF_EXCLUDED_ENTITY_IDS,
    CONF_EXCLUDED_DEVICE_IDS,
    CONF_STALENESS_EXCLUDED_DEVICE_IDS,
    CONF_INTERVAL_STORE_URL,
}

# The serialized result embeds the same identifying data per issue, so redact it
# too (matching the options redaction). ``detail`` can carry live entity values
# (watch-rule templates render {state}), and ``source``/``config_entry_id`` are
# identifying, so redact those as well for a shareable report.
_RESULT_REDACT = {
    "device_id",
    "entity_id",
    "name",
    "detail",
    "source",
    "config_entry_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VigilConfigEntry
) -> dict[str, Any]:
    """Return the resolved options and the most recent detection result."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data

    return {
        "options": async_redact_data(
            merged_options(entry.data, entry.options), _REDACT
        ),
        "last_update_success": coordinator.last_update_success,
        "result": async_redact_data(serialize_vigil_data(data), _RESULT_REDACT)
        if data is not None
        else None,
    }
