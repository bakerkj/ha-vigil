# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from custom_components.vigil.const import (
    CONF_AVAILABILITY_IGNORED_PLATFORMS,
    CONF_EXCLUDED_DEVICE_IDS,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITY_IDS,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_STALENESS_EXCLUDED_DEVICE_IDS,
    CONF_STALENESS_EXCLUDED_INTEGRATIONS,
)
from custom_components.vigil.detection.suppression import suppress_issues
from custom_components.vigil.models import (
    ExclusionConfig,
    IssueKind,
    VigilIssue,
    is_device_excluded,
)


def _issue(
    *,
    integration: str = "demo",
    device_id: str | None = None,
    domain: str | None = None,
) -> VigilIssue:
    return VigilIssue(
        kind=IssueKind.SILENT_DEVICE,
        name="Thing",
        integration=integration,
        detail="stale",
        device_id=device_id,
        domain=domain,
    )


def test_from_options_missing_keys_are_empty() -> None:
    config = ExclusionConfig.from_options({})
    assert config.domains == frozenset()
    assert config.entity_ids == frozenset()
    assert config.device_ids == frozenset()
    assert config.integrations == frozenset()
    assert config.ignored_platforms == frozenset()


def test_from_options_parses_lists() -> None:
    config = ExclusionConfig.from_options(
        {
            CONF_EXCLUDED_DOMAINS: ["sensor", "light"],
            CONF_EXCLUDED_ENTITY_IDS: ["sensor.x"],
            CONF_EXCLUDED_DEVICE_IDS: ["dev1", "dev2"],
            CONF_EXCLUDED_INTEGRATIONS: ["mqtt"],
            CONF_AVAILABILITY_IGNORED_PLATFORMS: ["annotation_notes", "fleet_meta"],
        }
    )
    assert config.domains == frozenset({"sensor", "light"})
    assert config.entity_ids == frozenset({"sensor.x"})
    assert config.device_ids == frozenset({"dev1", "dev2"})
    assert config.integrations == frozenset({"mqtt"})
    assert config.ignored_platforms == frozenset({"annotation_notes", "fleet_meta"})


def test_is_device_excluded_by_device() -> None:
    config = ExclusionConfig.from_options({CONF_EXCLUDED_DEVICE_IDS: ["dev1"]})
    assert is_device_excluded("dev1", "mqtt", config) is True
    assert is_device_excluded("dev2", "mqtt", config) is False
    assert is_device_excluded(None, None, config) is False


def test_is_device_excluded_by_integration() -> None:
    config = ExclusionConfig.from_options({CONF_EXCLUDED_INTEGRATIONS: ["mqtt"]})
    assert is_device_excluded("dev2", "mqtt", config) is True
    assert is_device_excluded("dev2", "zha", config) is False


def test_suppress_issues_startup_grace_returns_empty() -> None:
    issues = [_issue()]
    config = ExclusionConfig.from_options({})
    assert suppress_issues(issues, config, startup_grace_active=True) == []


def test_suppress_issues_drops_excluded_keeps_others() -> None:
    keep = _issue(integration="zha", device_id="good")
    drop_device = _issue(integration="zha", device_id="bad")
    drop_integration = _issue(integration="MQTT", device_id="good", domain="mqtt")
    config = ExclusionConfig.from_options(
        {
            CONF_EXCLUDED_DEVICE_IDS: ["bad"],
            CONF_EXCLUDED_INTEGRATIONS: ["mqtt"],
        }
    )
    result = suppress_issues(
        [keep, drop_device, drop_integration],
        config,
        startup_grace_active=False,
    )
    assert result == [keep]


def test_staleness_from_options_parses() -> None:
    config = ExclusionConfig.staleness_from_options(
        {
            CONF_STALENESS_EXCLUDED_INTEGRATIONS: ["acme_power"],
            CONF_STALENESS_EXCLUDED_DEVICE_IDS: ["devg"],
        }
    )
    assert config.integrations == frozenset({"acme_power"})
    assert config.device_ids == frozenset({"devg"})
    # domains / entity_ids are not part of the staleness scope.
    assert config.domains == frozenset()
    assert config.entity_ids == frozenset()


def test_staleness_exclusion_drops_silent_but_keeps_offline() -> None:
    """A staleness-excluded integration/device drops its SILENT_DEVICE issues but
    NOT its offline issues (offline detection stays intact)."""
    power_stale = VigilIssue(
        kind=IssueKind.SILENT_DEVICE,
        name="Idle channel",
        integration="Acme Power",
        detail="stale",
        device_id="p1",
        domain="acme_power",
    )
    power_offline = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_NO_SIGNAL,
        name="Power monitor",
        integration="Acme Power",
        detail="offline",
        device_id="p2",
        domain="acme_power",
    )
    other_stale = _issue(integration="zha", device_id="z1", domain="zha")
    dev_stale = _issue(integration="x", device_id="devx", domain="x")
    staleness = ExclusionConfig.staleness_from_options(
        {
            CONF_STALENESS_EXCLUDED_INTEGRATIONS: ["acme_power"],
            CONF_STALENESS_EXCLUDED_DEVICE_IDS: ["devx"],
        }
    )
    result = suppress_issues(
        [power_stale, power_offline, other_stale, dev_stale],
        ExclusionConfig.from_options({}),
        startup_grace_active=False,
        staleness_exclusions=staleness,
    )
    # Power-monitor stale dropped (integration) and devx stale dropped (device);
    # the power-monitor OFFLINE issue is KEPT; the unrelated stale is kept.
    assert result == [power_offline, other_stale]
