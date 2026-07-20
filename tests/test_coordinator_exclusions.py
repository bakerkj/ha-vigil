# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil.const import (
    CONF_EXCLUDED_DEVICE_IDS,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_STARTUP_IGNORE_SECONDS,
)
from custom_components.vigil.models import IssueKind
from tests.helpers import _failed_entry, _make_coordinator, _offline_device


async def test_excluded_integration_drops_failure(hass: HomeAssistant) -> None:
    """An excluded integration's config-entry failure never surfaces."""
    _failed_entry(hass)

    coordinator = await _make_coordinator(
        hass, options={CONF_EXCLUDED_INTEGRATIONS: ["demo"]}
    )
    data = await coordinator._async_update_data()

    assert data["counts"]["integration_failures"] == 0
    assert data["healthy"] is True
    # The excluded integration is also omitted from the health table.
    assert all(row["domain"] != "demo" for row in data["integration_health"])


async def test_excluded_device_drops_offline(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An excluded device id is dropped from the offline results."""
    demo = MockConfigEntry(domain="demo", title="Demo")
    demo.add_to_hass(hass)
    demo.mock_state(hass, ConfigEntryState.LOADED)
    device, _, _ = _offline_device(hass, demo, "excl1")

    coordinator = await _make_coordinator(
        hass,
        options={
            CONF_GRACE_PERIOD_MINUTES: 1,
            CONF_STARTUP_IGNORE_SECONDS: 0,
            CONF_EXCLUDED_DEVICE_IDS: [device.id],
        },
    )

    await coordinator._async_update_data()
    freezer.tick(timedelta(minutes=2))
    data = await coordinator._async_update_data()

    assert data["counts"]["devices_offline"] == 0
    assert data["healthy"] is True


async def test_failure_and_offline_in_one_cycle(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A failed integration and an offline device co-exist in one cycle.

    Verifies the combined counts and that integration_health groups by
    integration (domain): the two demo config entries collapse into a single
    "Demo" row carrying both the failure and the offline device.
    """
    failed = MockConfigEntry(domain="demo", title="Broken")
    failed.add_to_hass(hass)
    failed.mock_state(hass, ConfigEntryState.SETUP_ERROR)

    healthy = MockConfigEntry(domain="demo", title="Working")
    healthy.add_to_hass(hass)
    healthy.mock_state(hass, ConfigEntryState.LOADED)
    device, _, _ = _offline_device(hass, healthy, "combo1")

    coordinator = await _make_coordinator(
        hass,
        options={CONF_GRACE_PERIOD_MINUTES: 1, CONF_STARTUP_IGNORE_SECONDS: 0},
    )

    await coordinator._async_update_data()
    freezer.tick(timedelta(minutes=2))
    data = await coordinator._async_update_data()

    assert data["counts"]["integration_failures"] == 1
    assert data["counts"]["devices_offline"] == 1
    assert data["counts"]["total"] == 2
    assert data["healthy"] is False

    rows = {row["domain"]: row for row in data["integration_health"]}
    demo_row = rows["demo"]
    # Grouped by integration, not per config-entry title.
    assert demo_row["title"] not in ("Broken", "Working")
    assert demo_row["failed"] is True
    assert demo_row["healthy"] is False
    assert demo_row["offline_count"] == 1
    assert demo_row["device_count"] == 1

    # The offline issue records the owning device id, with a friendly integration.
    offline_issue = data["devices_offline"][0]
    assert offline_issue.device_id == device.id
    assert offline_issue.kind is IssueKind.DEVICE_OFFLINE_CONFIRMED
    assert offline_issue.domain == "demo"
