# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The detection pipeline — the composition root for 'what is unhealthy'.

Given a :class:`.context.CycleContext`, run the five engines, apply suppression,
resolve display names, and assemble the :class:`VigilData` payload. Pure
composition over the snapshot; the coordinator owns the surrounding concerns.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.loader import async_get_integrations

from .const import DOMAIN
from .detection.engines.engine1_config_entry import detect_config_entry_issues
from .detection.engines.engine2_unavailability import detect_unavailability_issues
from .detection.engines.engine3_staleness import detect_staleness_issues
from .detection.engines.engine4_watch_rules import detect_watch_issues
from .detection.engines.engine5_apps import detect_app_issues
from .detection.suppression import suppress_issues
from .models import IssueKind, VigilData, build_vigil_data
from .reporting.health import build_integration_health

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .context import CycleContext
    from .models import DeviceTuple, VigilIssue

_LOGGER = logging.getLogger(__name__)


async def run_detection(ctx: CycleContext) -> VigilData:
    """Compose the five engines + suppression + rollup into one ``VigilData``.

    Stateless: everything it needs arrives on the :class:`CycleContext`.
    """
    # Engine 1 — config entry health (authoritative, no grace).
    engine1 = detect_config_entry_issues(ctx.hass, exclusions=ctx.exclusions)
    flagged_entry_ids = {
        issue.config_entry_id for issue in engine1 if issue.config_entry_id
    }

    # Engine 2 — device unavailability (grace-gated). Reads/GCs ctx.downtime.
    engine2 = detect_unavailability_issues(
        ctx.tuples,
        flagged_entry_ids=flagged_entry_ids,
        grace_period=ctx.grace_period,
        battery_multiplier=ctx.battery_multiplier,
        downtime=ctx.downtime,
        now=ctx.now,
        boot_time=ctx.boot_time,
        # GC downtime only for devices gone from the registry, not ones merely
        # missing from this cycle's tuples (mid-reconnect after a restart).
        known_device_ids=set(dr.async_get(ctx.hass).devices),
    )

    # Engine 3 — staleness (learning-gated). Observes into ctx.learner.
    engine3 = detect_staleness_issues(
        ctx.tuples,
        learner=ctx.learner,
        multiplier=ctx.staleness_multiplier,
        now=ctx.now,
        boot_time=ctx.boot_time,
    )

    # Engine 4 — declarative watch rules. A user-authored rules file must
    # never take detection down, so isolate load+evaluate: on failure, skip
    # this cycle's faults and keep engines 1-3.
    try:
        watch_rules = await ctx.config_store.async_get_watch_rules()
        engine4 = detect_watch_issues(
            ctx.hass,
            ctx.tuples,
            rules=watch_rules,
            fault_state=ctx.fault_state,
            now=ctx.now,
        )
    except Exception:  # noqa: BLE001 - watch rules are user config; never fatal
        _LOGGER.exception(
            "Vigil: watch-rule evaluation failed; skipping device faults this cycle"
        )
        engine4 = []

    # Engine 5 — Supervisor app health. ``ctx.apps is None`` means the
    # Supervisor read FAILED this cycle: skip detection entirely and leave
    # app_health untouched, so a transient blip doesn't wipe flap/since state.
    if ctx.apps is None:
        engine5 = []
    else:
        engine5 = detect_app_issues(
            ctx.apps,
            history=ctx.app_health,
            now=ctx.now,
            excluded=ctx.exclusions.apps,
        )

    # Layer 4 — suppression (startup grace + exclusion safety net).
    issues = suppress_issues(
        engine1 + engine2 + engine3 + engine4 + engine5,
        ctx.exclusions,
        startup_grace_active=ctx.startup_grace_active,
        staleness_exclusions=ctx.staleness_exclusions,
    )

    # Resolve friendly integration names and stamp them onto each issue for
    # display. issue.domain stays the stable key; only the label changes.
    name_map = await _resolve_integration_names(ctx.hass, ctx.tuples, issues)
    for issue in issues:
        if issue.domain:
            friendly = name_map.get(issue.domain, issue.integration)
            issue.integration = friendly
            # Name an integration failure by its single linked device, else
            # the friendly integration name (see _failure_display_name).
            if issue.kind is IssueKind.INTEGRATION_FAILURE:
                issue.name = _failure_display_name(ctx.hass, issue, friendly)

    # Stable display order: group by integration, then device name
    # (case-folded). Section lists and the card render in this order.
    issues.sort(
        key=lambda i: (
            (i.integration or i.domain or "").lower(),
            (i.name or "").lower(),
        )
    )

    return build_vigil_data(
        issues=issues,
        integration_health=build_integration_health(
            ctx.hass,
            ctx.tuples,
            issues,
            ctx.exclusions,
            name_map,
            startup_grace_active=ctx.startup_grace_active,
        ),
        last_run=ctx.now,
        startup_grace_active=ctx.startup_grace_active,
    )


def _failure_display_name(hass: HomeAssistant, issue: VigilIssue, friendly: str) -> str:
    """Name an integration failure by its single linked device, else the friendly
    integration name.

    A ``setup_retry`` entry often reverts its title to the generic integration
    default while the linked device keeps its real name, so prefer that when the
    entry owns exactly one named device.
    """
    if issue.config_entry_id is None:
        return friendly
    dev_reg = dr.async_get(hass)
    named: list[str] = []
    for d in dr.async_entries_for_config_entry(dev_reg, issue.config_entry_id):
        n = d.name_by_user or d.name
        if n:
            named.append(n)
    return named[0] if len(named) == 1 else friendly


async def _resolve_integration_names(
    hass: HomeAssistant,
    tuples: list[DeviceTuple],
    issues: list[VigilIssue],
) -> dict[str, str]:
    """Map config-entry domains to friendly integration names for display.

    e.g. ``esphome`` -> ``ESPHome``. Falls back to the raw domain if the
    integration manifest can't be resolved.
    """
    domains: set[str] = {i.domain for i in issues if i.domain}
    domains |= {t.config_entry_domain for t in tuples if t.config_entry_domain}
    for entry in hass.config_entries.async_entries():
        if entry.domain != DOMAIN:
            domains.add(entry.domain)
    if not domains:
        return {}
    resolved = await async_get_integrations(hass, domains)
    return {
        domain: (integration.name if not isinstance(integration, Exception) else domain)
        for domain, integration in resolved.items()
    }
