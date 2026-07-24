# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

import json
import types
from datetime import UTC, datetime
from typing import cast

import pytest

from custom_components.vigil.models import (
    IntegrationHealthRow,
    IssueKind,
    VigilData,
    VigilIssue,
)
from custom_components.vigil.reporting.serialize import serialize_vigil_data
from custom_components.vigil.sensor import (
    SENSOR_DESCRIPTIONS,
    VigilSensor,
    VigilStateSensor,
)

LAST_RUN = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
ENTRY_ID = "test_entry"


def _build_data() -> VigilData:
    integration_failure = VigilIssue(
        kind=IssueKind.INTEGRATION_FAILURE,
        name="Demo",
        integration="demo",
        detail="setup_error since startup",
        since=None,
        source="setup_error",
        config_entry_id="abc",
    )
    offline = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_CONFIRMED,
        name="Front Door",
        integration="zwave_js",
        detail="node dead",
        since=datetime(2026, 6, 26, 11, 0, 0, tzinfo=UTC),
        source="zwave",
        device_id="dev1",
    )
    stale = VigilIssue(
        kind=IssueKind.SILENT_DEVICE,
        name="Kitchen Temp",
        integration="mqtt",
        detail="no updates",
        since=datetime(2026, 6, 26, 10, 0, 0, tzinfo=UTC),
        source="staleness",
        device_id="dev2",
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
        issues=[integration_failure, offline, stale],
        integration_failures=[integration_failure],
        devices_offline=[offline],
        stale_devices=[stale],
        device_faults=[],
        app_issues=[],
        counts={
            "total": 3,
            "integration_failures": 1,
            "devices_offline": 1,
            "stale_devices": 1,
            "device_faults": 0,
            "app_issues": 0,
        },
        integration_health=[health],
        last_run=LAST_RUN,
        healthy=False,
        startup_grace_active=False,
    )


def _fake_coordinator(data: VigilData | None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        data=data,
        last_update_success=True,
        async_add_listener=lambda *args, **kwargs: lambda: None,
    )


@pytest.mark.parametrize("description", SENSOR_DESCRIPTIONS)
def test_native_value(description) -> None:  # type: ignore[no-untyped-def]
    data = _build_data()
    coordinator = _fake_coordinator(data)
    sensor = VigilSensor(ENTRY_ID, coordinator, description)  # type: ignore[arg-type]

    assert sensor.native_value == data["counts"][description.count_key]
    assert sensor.unique_id == f"{ENTRY_ID}_{description.key}"


@pytest.mark.parametrize("description", SENSOR_DESCRIPTIONS)
def test_count_sensors_carry_no_heavy_attributes(description) -> None:  # type: ignore[no-untyped-def]
    """Count sensors are recorded, so they must NOT carry the issue payload."""
    data = _build_data()
    sensor = VigilSensor(ENTRY_ID, _fake_coordinator(data), description)  # type: ignore[arg-type]

    assert sensor.extra_state_attributes is None


def test_state_sensor_summary_and_status() -> None:
    """The state sensor exposes only a small summary (counts + health), NOT the
    full per-issue payload — that lives on the HTTP API to stay under the
    recorder's 16 KB attribute limit."""
    data = _build_data()
    sensor = VigilStateSensor(ENTRY_ID, _fake_coordinator(data))  # type: ignore[arg-type]

    assert sensor.unique_id == f"{ENTRY_ID}_state"
    assert sensor.native_value == "issues"  # not healthy

    attributes = sensor.extra_state_attributes
    assert attributes is not None
    # Summary only — no heavy lists that could blow the 16 KB limit.
    assert "issues" not in attributes
    assert "integration_health" not in attributes
    assert attributes["healthy"] is False
    assert attributes["total_issues"] == 3
    assert attributes["devices_offline"] == 1
    assert attributes["integration_failures"] == 1
    assert attributes["stale_devices"] == 1
    assert attributes["startup_grace_active"] is False


def test_state_sensor_status_values() -> None:
    """Status string reflects healthy / issues / starting."""
    data = _build_data()

    healthy = cast(VigilData, {**data, "healthy": True, "counts": {"total": 0}})
    assert VigilStateSensor(ENTRY_ID, _fake_coordinator(healthy)).native_value == "ok"  # type: ignore[arg-type]

    starting = cast(VigilData, {**data, "startup_grace_active": True})
    sensor = VigilStateSensor(ENTRY_ID, _fake_coordinator(starting))  # type: ignore[arg-type]
    assert sensor.native_value == "starting"


def test_native_value_no_data() -> None:
    description = SENSOR_DESCRIPTIONS[0]
    sensor = VigilSensor(ENTRY_ID, _fake_coordinator(None), description)  # type: ignore[arg-type]
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None

    state_sensor = VigilStateSensor(ENTRY_ID, _fake_coordinator(None))  # type: ignore[arg-type]
    assert state_sensor.native_value is None
    assert state_sensor.extra_state_attributes is None


def test_serialize_vigil_data_is_json_serializable() -> None:
    data = _build_data()
    serialized = serialize_vigil_data(data)

    # Must be JSON-serializable end to end (raises here if any field isn't).
    json.dumps(serialized)

    assert isinstance(serialized["last_run"], str)
    assert serialized["last_run"] == LAST_RUN.isoformat()
    assert serialized["counts"] == data["counts"]
    assert serialized["healthy"] is False
    assert serialized["startup_grace_active"] is False
    assert serialized["integration_health"] == data["integration_health"]

    for key in (
        "issues",
        "integration_failures",
        "devices_offline",
        "stale_devices",
        "device_faults",
    ):
        assert all(isinstance(item, dict) for item in serialized[key])
    assert len(serialized["issues"]) == 3
