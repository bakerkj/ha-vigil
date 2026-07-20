# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Direct tests for the per-integration health rollup (reporting/health.py)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil.models import (
    IntegrationHealthRow,
    IssueKind,
    VigilIssue,
)
from custom_components.vigil.reporting.health import build_integration_health
from tests.helpers import NO_EXCLUSIONS


def _entry(
    hass: HomeAssistant, domain: str, state: ConfigEntryState
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=domain, title=domain)
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    return entry


def _row(rows: list[IntegrationHealthRow], domain: str) -> IntegrationHealthRow:
    return next(r for r in rows if r["domain"] == domain)


def _integration_failure(entry_id: str) -> VigilIssue:
    return VigilIssue(
        kind=IssueKind.INTEGRATION_FAILURE,
        name="Demo",
        integration="d",
        detail="setup_error since startup",
        config_entry_id=entry_id,
        domain="d",
    )


def _offline_issue(_entry_id: str) -> VigilIssue:
    return VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_NO_SIGNAL,
        name="Sensor",
        integration="d",
        detail="offline",
        device_id="dev1",
        domain="d",
    )


@pytest.mark.parametrize(
    (
        "entry_state",
        "issue_factory",
        "exp_state",
        "exp_healthy",
        "exp_failed",
        "exp_offline",
    ),
    [
        # Transient non-alert state → 'loaded' + healthy (no contradiction).
        (ConfigEntryState.FAILED_UNLOAD, None, "loaded", True, False, 0),
        # Alert-state entry with its issue → 'N/M not loaded' + unhealthy + failed.
        (
            ConfigEntryState.SETUP_ERROR,
            _integration_failure,
            "1/1 not loaded",
            False,
            True,
            0,
        ),
        # Offline device issue → unhealthy with an offline_count; state stays 'loaded'.
        (ConfigEntryState.LOADED, _offline_issue, "loaded", False, False, 1),
    ],
)
async def test_integration_health_row(
    hass: HomeAssistant,
    entry_state: ConfigEntryState,
    issue_factory: Callable[[str], VigilIssue] | None,
    exp_state: str,
    exp_healthy: bool,
    exp_failed: bool,
    exp_offline: int,
) -> None:
    entry = _entry(hass, "d", entry_state)
    issues = [issue_factory(entry.entry_id)] if issue_factory is not None else []
    row = _row(build_integration_health(hass, [], issues, NO_EXCLUSIONS, {}), "d")
    assert row["state"] == exp_state
    assert row["healthy"] is exp_healthy
    assert row["failed"] is exp_failed
    assert row["offline_count"] == exp_offline
