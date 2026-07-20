# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The per-integration health view-model.

Rolls the cycle's issues up into one row per integration (domain) for the
dashboard card / HTTP feed.
"""

from __future__ import annotations

from collections import Counter

from homeassistant.core import HomeAssistant, callback

from ..models import (
    CONFIG_ENTRY_ALERT_STATES,
    OFFLINE_KINDS,
    DeviceTuple,
    ExclusionConfig,
    IntegrationHealthRow,
    IssueKind,
    VigilIssue,
    config_entry_is_reportable,
)


@callback
def build_integration_health(
    hass: HomeAssistant,
    tuples: list[DeviceTuple],
    issues: list[VigilIssue],
    exclusions: ExclusionConfig,
    name_map: dict[str, str],
    *,
    startup_grace_active: bool = False,
) -> list[IntegrationHealthRow]:
    """Aggregate health into one row per *integration* (domain), not per entry.

    Per-device integrations create many config entries titled after the
    device/account; grouping by domain shows a single row with summed counts.

    During the startup grace all issues are suppressed, so every row is
    ``healthy``; the ``state`` string reads "loaded" to match rather than
    contradicting it with a live "not loaded" count for an entry still settling.
    """
    device_count: Counter[str] = Counter(
        t.config_entry_domain for t in tuples if t.config_entry_domain
    )

    failed: set[str] = set()
    offline_count: Counter[str] = Counter()
    stale_count: Counter[str] = Counter()
    fault_count: Counter[str] = Counter()
    for issue in issues:
        domain = issue.domain
        if domain is None:
            continue
        if issue.kind is IssueKind.INTEGRATION_FAILURE:
            failed.add(domain)
        elif issue.kind in OFFLINE_KINDS:
            offline_count[domain] += 1
        elif issue.kind is IssueKind.SILENT_DEVICE:
            stale_count[domain] += 1
        elif issue.kind is IssueKind.DEVICE_FAULT:
            fault_count[domain] += 1

    entry_total: Counter[str] = Counter()
    entry_failed: Counter[str] = Counter()
    for entry in hass.config_entries.async_entries():
        if not config_entry_is_reportable(entry, exclusions):
            continue
        entry_total[entry.domain] += 1
        # Count as "not loaded" only the states Engine 1 flags, so the row's
        # ``state`` agrees with its ``healthy`` flag.
        if entry.state in CONFIG_ENTRY_ALERT_STATES:
            entry_failed[entry.domain] += 1

    domains = (
        set(entry_total)
        | set(device_count)
        | failed
        | set(offline_count)
        | set(stale_count)
        | set(fault_count)
    )

    rows: list[IntegrationHealthRow] = []
    for domain in domains:
        is_failed = domain in failed
        offline = offline_count[domain]
        stale = stale_count[domain]
        faults = fault_count[domain]
        bad_entries = entry_failed[domain]
        total_entries = entry_total[domain]
        if startup_grace_active or bad_entries == 0:
            state = "loaded"
        else:
            state = f"{bad_entries}/{total_entries} not loaded"
        rows.append(
            IntegrationHealthRow(
                domain=domain,
                title=name_map.get(domain, domain),
                state=state,
                healthy=not (is_failed or offline or stale or faults),
                device_count=device_count[domain],
                offline_count=offline,
                stale_count=stale,
                fault_count=faults,
                failed=is_failed,
            )
        )

    rows.sort(key=lambda r: (r["healthy"], r["title"].lower()))
    return rows
