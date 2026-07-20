# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil import VigilEntryData
from custom_components.vigil.const import (
    CONF_INTERVAL_STORE_URL,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.vigil.diagnostics import async_get_config_entry_diagnostics
from custom_components.vigil.models import VigilData


def _data(now: Any) -> VigilData:
    return {
        "issues": [],
        "integration_failures": [],
        "devices_offline": [],
        "stale_devices": [],
        "device_faults": [],
        "app_issues": [],
        "counts": {
            "total": 0,
            "integration_failures": 0,
            "devices_offline": 0,
            "stale_devices": 0,
            "device_faults": 0,
            "app_issues": 0,
        },
        "integration_health": [],
        "last_run": now,
        "healthy": True,
        "startup_grace_active": False,
    }


def _entry_with_data(
    hass: HomeAssistant,
    data: VigilData | None,
    *,
    options: dict[str, Any] | None = None,
    success: bool = True,
) -> MockConfigEntry:
    """A Vigil entry whose coordinator carries ``data`` — the diagnostics fixture."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options or {})
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(data=data, last_update_success=success)
    entry.runtime_data = VigilEntryData(coordinator=coordinator)  # type: ignore[arg-type]
    return entry


async def test_diagnostics_returns_options_and_result(hass: HomeAssistant) -> None:
    from homeassistant.util import dt as dt_util

    entry = _entry_with_data(
        hass, _data(dt_util.utcnow()), options={CONF_SCAN_INTERVAL: 30}
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["options"][CONF_SCAN_INTERVAL] == 30
    assert diag["last_update_success"] is True
    assert diag["result"]["healthy"] is True
    json.dumps(diag)  # must be JSON-serializable for the download button


async def test_diagnostics_redacts_interval_store_url(hass: HomeAssistant) -> None:
    """The credential-bearing interval-store URL must never leak in diagnostics."""
    from homeassistant.util import dt as dt_util

    secret_url = "mysql+pymysql://vigil:SUPERSECRET@10.0.0.5/vigil"
    entry = _entry_with_data(
        hass, _data(dt_util.utcnow()), options={CONF_INTERVAL_STORE_URL: secret_url}
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["options"][CONF_INTERVAL_STORE_URL] != secret_url
    assert "SUPERSECRET" not in json.dumps(diag)


async def test_diagnostics_redacts_ids_in_result(hass: HomeAssistant) -> None:
    """Device/entity ids and device names in the serialized result are redacted too."""
    from homeassistant.util import dt as dt_util

    from custom_components.vigil.models import IssueKind, VigilIssue

    now = dt_util.utcnow()
    data = _data(now)
    fault = VigilIssue(
        kind=IssueKind.DEVICE_FAULT,
        name="Kitchen Zigbee Plug",
        integration="mqtt",
        detail="Not OK: fault",
        since=now,
        source="rule",
        device_id="dev-secret-123",
        entity_id="sensor.kitchen_plug_status",
        config_entry_id="entry1",
        domain="mqtt",
    )
    data["issues"] = [fault]
    data["device_faults"] = [fault]
    data["counts"]["total"] = 1
    data["counts"]["device_faults"] = 1

    entry = _entry_with_data(hass, data)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    blob = json.dumps(diag)
    assert "dev-secret-123" not in blob
    assert "sensor.kitchen_plug_status" not in blob
    assert "Kitchen Zigbee Plug" not in blob
    # The non-identifying fields still come through.
    assert diag["result"]["counts"]["total"] == 1


async def test_diagnostics_handles_no_result(hass: HomeAssistant) -> None:
    entry = _entry_with_data(hass, None, success=False)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["result"] is None
    assert diag["last_update_success"] is False
