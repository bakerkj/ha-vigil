# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil.config_flow import DEFAULTS
from custom_components.vigil.const import (
    CONF_AVAILABILITY_IGNORED_PLATFORMS,
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_ENABLE_APP_MONITORING,
    CONF_ENABLE_NOTIFICATION,
    CONF_EXCLUDED_APPS,
    CONF_EXCLUDED_DEVICE_IDS,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITY_IDS,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_INTERVAL_STORE_URL,
    CONF_RECORDER_LOOKBACK_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_STALENESS_EXCLUDED_DEVICE_IDS,
    CONF_STALENESS_EXCLUDED_INTEGRATIONS,
    CONF_STALENESS_MULTIPLIER,
    CONF_STARTUP_IGNORE_SECONDS,
    DOMAIN,
    NAME,
    UNIQUE_ID,
)

_REQUIRED_KEYS = (
    CONF_SCAN_INTERVAL,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_STALENESS_MULTIPLIER,
    CONF_STARTUP_IGNORE_SECONDS,
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_RECORDER_LOOKBACK_DAYS,
    CONF_ENABLE_NOTIFICATION,
    CONF_ENABLE_APP_MONITORING,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_EXCLUDED_ENTITY_IDS,
    CONF_EXCLUDED_DEVICE_IDS,
    CONF_STALENESS_EXCLUDED_INTEGRATIONS,
    CONF_STALENESS_EXCLUDED_DEVICE_IDS,
    CONF_AVAILABILITY_IGNORED_PLATFORMS,
    CONF_EXCLUDED_APPS,
)


@pytest.fixture(autouse=True)
def bypass_setup() -> Iterator[None]:
    """Isolate the config flow from full integration setup (frontend deps, boot)."""
    with (
        patch(
            "homeassistant.config_entries.async_process_deps_reqs",
            return_value=None,
        ),
        patch(
            "custom_components.vigil.async_setup_entry",
            return_value=True,
        ),
    ):
        yield


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The user step shows a form, then creates the single entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(DEFAULTS)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME

    data = result["data"]
    for key in _REQUIRED_KEYS:
        assert key in data


async def test_single_instance_aborts(hass: HomeAssistant) -> None:
    """A second setup attempt aborts because Vigil is single-instance."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        unique_id=UNIQUE_ID,
        data=dict(DEFAULTS),
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow_updates_data(hass: HomeAssistant) -> None:
    """The options flow shows a form and persists changed values."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        unique_id=UNIQUE_ID,
        data=dict(DEFAULTS),
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    changed: dict[str, Any] = {
        CONF_SCAN_INTERVAL: 120,
        CONF_GRACE_PERIOD_MINUTES: 30,
        CONF_STALENESS_MULTIPLIER: 5.0,
        CONF_STARTUP_IGNORE_SECONDS: 600,
        CONF_BATTERY_GRACE_MULTIPLIER: 3.0,
        CONF_RECORDER_LOOKBACK_DAYS: 14,
        CONF_ENABLE_NOTIFICATION: False,
        CONF_ENABLE_APP_MONITORING: False,
        CONF_EXCLUDED_DOMAINS: ["sun"],
        CONF_EXCLUDED_INTEGRATIONS: ["demo"],
        CONF_EXCLUDED_ENTITY_IDS: ["sensor.example"],
        CONF_EXCLUDED_DEVICE_IDS: ["abc123"],
        CONF_STALENESS_EXCLUDED_INTEGRATIONS: ["acme_power"],
        CONF_STALENESS_EXCLUDED_DEVICE_IDS: ["powerdev"],
        CONF_AVAILABILITY_IGNORED_PLATFORMS: ["annotation_notes", "fleet_meta"],
        CONF_EXCLUDED_APPS: ["a0d7b954_glances"],
        CONF_INTERVAL_STORE_URL: "mysql+pymysql://u:p@h/db",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], changed
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == changed


async def test_options_flow_persists_ints_not_floats(hass: HomeAssistant) -> None:
    """Integer knobs submitted as floats (NumberSelector) persist as int; float knobs stay float."""
    entry = MockConfigEntry(
        domain=DOMAIN, title=NAME, unique_id=UNIQUE_ID, data=dict(DEFAULTS)
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    submitted = dict(DEFAULTS)
    submitted[CONF_SCAN_INTERVAL] = 120.0
    submitted[CONF_GRACE_PERIOD_MINUTES] = 30.0
    submitted[CONF_STARTUP_IGNORE_SECONDS] = 600.0
    submitted[CONF_RECORDER_LOOKBACK_DAYS] = 14.0

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submitted
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    for key in (
        CONF_SCAN_INTERVAL,
        CONF_GRACE_PERIOD_MINUTES,
        CONF_STARTUP_IGNORE_SECONDS,
        CONF_RECORDER_LOOKBACK_DAYS,
    ):
        assert type(data[key]) is int, f"{key} persisted as {type(data[key])}"
    # The multiplier knobs are genuinely float and must stay float.
    assert type(data[CONF_STALENESS_MULTIPLIER]) is float
