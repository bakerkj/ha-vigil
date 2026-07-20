# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from homeassistant.core import HomeAssistant

from ...models import (
    CONFIG_ENTRY_ALERT_STATES,
    ExclusionConfig,
    IssueKind,
    VigilIssue,
    config_entry_is_reportable,
)


def detect_config_entry_issues(
    hass: HomeAssistant, *, exclusions: ExclusionConfig
) -> list[VigilIssue]:
    """Engine 1 — flag config entries stuck in a failed/unloaded state."""
    issues: list[VigilIssue] = []
    for entry in hass.config_entries.async_entries():
        if not config_entry_is_reportable(entry, exclusions):
            continue
        if entry.state in CONFIG_ENTRY_ALERT_STATES:
            issues.append(
                VigilIssue(
                    kind=IssueKind.INTEGRATION_FAILURE,
                    name=entry.title or entry.domain,
                    integration=entry.domain,
                    detail=f"{entry.state.value} since startup",
                    source=entry.state.value,
                    config_entry_id=entry.entry_id,
                    domain=entry.domain,
                    since=None,
                )
            )
    return issues
