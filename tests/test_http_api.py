# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

import json
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil import VigilEntryData
from custom_components.vigil.const import DOMAIN
from custom_components.vigil.http_api import VigilStateView
from custom_components.vigil.models import (
    IntegrationHealthRow,
    IssueKind,
    VigilData,
    VigilIssue,
)
from custom_components.vigil.reporting.serialize import serialize_vigil_data

LAST_RUN = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)


def _build_data() -> VigilData:
    failure = VigilIssue(
        kind=IssueKind.INTEGRATION_FAILURE,
        name="Demo",
        integration="demo",
        detail="setup_error since startup",
        since=None,
        source="setup_error",
        config_entry_id="abc",
        domain="demo",
    )
    offline = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_CONFIRMED,
        name="Front Door",
        integration="zwave_js",
        detail="node dead",
        since=datetime(2026, 6, 26, 11, 0, 0, tzinfo=UTC),
        source="zwave",
        device_id="dev1",
        domain="zwave_js",
    )
    health: IntegrationHealthRow = {
        "domain": "demo",
        "title": "Demo",
        "state": "setup_error",
        "healthy": False,
        "device_count": 0,
        "offline_count": 0,
        "stale_count": 0,
        "fault_count": 0,
        "failed": True,
    }
    return VigilData(
        issues=[failure, offline],
        integration_failures=[failure],
        devices_offline=[offline],
        stale_devices=[],
        device_faults=[],
        app_issues=[],
        counts={
            "total": 2,
            "integration_failures": 1,
            "devices_offline": 1,
            "stale_devices": 0,
            "device_faults": 0,
            "app_issues": 0,
        },
        integration_health=[health],
        last_run=LAST_RUN,
        healthy=False,
        startup_grace_active=False,
    )


def _decode(response: Any) -> dict[str, Any]:
    """Decode the JSON body of an aiohttp ``Response``."""
    body: bytes = response.body
    decoded: dict[str, Any] = json.loads(body)
    return decoded


# --- serialize_vigil_data ----------------------------------------------------


def test_serialize_includes_domain_and_isoformat() -> None:
    """Each serialized issue carries a ``domain`` key; last_run is an isoformat str."""
    serialized = serialize_vigil_data(_build_data())

    assert isinstance(json.dumps(serialized), str)
    assert isinstance(serialized["last_run"], str)
    assert serialized["last_run"] == LAST_RUN.isoformat()

    for key in (
        "issues",
        "integration_failures",
        "devices_offline",
        "stale_devices",
        "device_faults",
    ):
        for item in serialized[key]:
            assert isinstance(item, dict)
            assert "domain" in item

    assert serialized["issues"][0]["domain"] == "demo"
    assert serialized["devices_offline"][0]["domain"] == "zwave_js"


# --- VigilStateView ----------------------------------------------------------


def _add_entry(hass: HomeAssistant, coordinator: object) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entry.runtime_data = VigilEntryData(coordinator=coordinator)  # type: ignore[arg-type]


async def test_view_returns_populated_state(hass: HomeAssistant) -> None:
    _add_entry(hass, types.SimpleNamespace(data=_build_data()))

    view = VigilStateView(hass)
    payload = _decode(await view.get(MagicMock()))

    assert payload["healthy"] is False
    assert payload["counts"]["total"] == 2
    assert payload["last_run"] == LAST_RUN.isoformat()
    assert len(payload["issues"]) == 2


# The starting fallback must have the same key set as a real payload and must not
# claim health before a cycle has run.
_POPULATED_KEYS = set(serialize_vigil_data(_build_data()))


def _assert_starting_fallback(payload: dict[str, Any]) -> None:
    assert set(payload) == _POPULATED_KEYS
    assert payload["healthy"] is False
    assert payload["startup_grace_active"] is True
    assert payload["counts"]["total"] == 0
    assert payload["issues"] == []
    for key in (
        "integration_failures",
        "devices_offline",
        "stale_devices",
        "device_faults",
    ):
        assert payload[key] == []
    assert payload["integration_health"] == []


async def test_view_breaks_to_fallback_when_data_none(hass: HomeAssistant) -> None:
    """A present entry whose coordinator has no data yields the starting fallback."""
    _add_entry(hass, types.SimpleNamespace(data=None))

    view = VigilStateView(hass)
    _assert_starting_fallback(_decode(await view.get(MagicMock())))


async def test_view_fallback_when_runtime_data_not_vigil(hass: HomeAssistant) -> None:
    """Non-VigilEntryData runtime_data hits the isinstance guard, not just None."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entry.runtime_data = object()  # present, but not VigilEntryData

    view = VigilStateView(hass)
    _assert_starting_fallback(_decode(await view.get(MagicMock())))


async def test_view_fallback_when_no_entry(hass: HomeAssistant) -> None:
    """No Vigil config entry also returns the starting fallback."""
    view = VigilStateView(hass)
    _assert_starting_fallback(_decode(await view.get(MagicMock())))
