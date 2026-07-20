# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

import pytest
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil.detection.engines.engine1_config_entry import (
    detect_config_entry_issues,
)
from custom_components.vigil.models import IssueKind
from custom_components.vigil.models import ExclusionConfig

EMPTY = ExclusionConfig(
    domains=frozenset(),
    entity_ids=frozenset(),
    device_ids=frozenset(),
    integrations=frozenset(),
)


def _add_entry(
    hass: HomeAssistant,
    *,
    domain: str,
    state: ConfigEntryState,
    title: str = "",
    disabled: bool = False,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=domain,
        title=title,
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    return entry


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.MIGRATION_ERROR,
    ],
)
async def test_alert_state_fires(hass: HomeAssistant, state: ConfigEntryState) -> None:
    entry = _add_entry(hass, domain="demo", state=state, title="Demo")

    issues = detect_config_entry_issues(hass, exclusions=EMPTY)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == IssueKind.INTEGRATION_FAILURE
    assert issue.name == "Demo"
    assert issue.integration == "demo"
    assert issue.source == state.value
    assert issue.detail == f"{state.value} since startup"
    assert issue.config_entry_id == entry.entry_id
    assert issue.since is None


async def test_loaded_does_not_fire(hass: HomeAssistant) -> None:
    _add_entry(hass, domain="demo", state=ConfigEntryState.LOADED)

    assert detect_config_entry_issues(hass, exclusions=EMPTY) == []


async def test_name_falls_back_to_domain(hass: HomeAssistant) -> None:
    _add_entry(hass, domain="demo", state=ConfigEntryState.SETUP_ERROR, title="")

    issues = detect_config_entry_issues(hass, exclusions=EMPTY)

    assert issues[0].name == "demo"


async def test_excluded_integration_skipped(hass: HomeAssistant) -> None:
    _add_entry(hass, domain="demo", state=ConfigEntryState.SETUP_ERROR)
    exclusions = ExclusionConfig(
        domains=frozenset(),
        entity_ids=frozenset(),
        device_ids=frozenset(),
        integrations=frozenset({"demo"}),
    )

    assert detect_config_entry_issues(hass, exclusions=exclusions) == []


async def test_ignored_entry_skipped(hass: HomeAssistant) -> None:
    """Ignored discovery entries are NOT_LOADED by design and must not fire.

    These dominate a real install (every ignored BLE/discovery entry), so
    flagging them would bury the genuine failures.
    """
    entry = MockConfigEntry(domain="led_ble", title="Ignored", source=SOURCE_IGNORE)
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)

    assert detect_config_entry_issues(hass, exclusions=EMPTY) == []


async def test_disabled_entry_skipped(hass: HomeAssistant) -> None:
    _add_entry(
        hass,
        domain="demo",
        state=ConfigEntryState.SETUP_ERROR,
        disabled=True,
    )

    assert detect_config_entry_issues(hass, exclusions=EMPTY) == []
