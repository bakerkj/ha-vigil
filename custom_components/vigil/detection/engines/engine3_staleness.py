# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_CLASS

from ...const import NO_VALUE_STATES
from ...models import ConnectivityState, DeviceTuple, IssueKind, VigilIssue

# Floor for a per-entity expected cadence so a degenerate 0 can't flag forever.
_MIN_EXPECTED_INTERVAL = 1.0


class IntervalLearnerProtocol(Protocol):
    """The slice of ``IntervalLearner`` that Engine 3 depends on."""

    def is_populated(self, entity_id: str) -> bool: ...

    def expected_interval(
        self, entity_id: str, domain: str, device_class: str | None
    ) -> float: ...

    def observe(self, entity_id: str, timestamp: datetime) -> None: ...


def detect_staleness_issues(
    tuples: list[DeviceTuple],
    *,
    learner: IntervalLearnerProtocol,
    multiplier: float,
    now: datetime,
    boot_time: datetime | None = None,
) -> list[VigilIssue]:
    """Engine 3 — flag silent devices: reachable but no data flowing.

    A device is "silent" only when EVERY reporting entity is overdue against its
    OWN learned cadence (``multiplier * expected_interval``). Liveness uses
    ``last_reported``, so an unchanging-but-still-reporting entity keeps its
    device alive.

    Runs for connectivity UP and UNKNOWN (a hard DOWN is Engine 2's job) — a
    silently-dead entity holding a stale value is not ``unavailable``, so only
    staleness catches it. Requires the config entry LOADED and the device not
    already all unavailable.
    """
    issues: list[VigilIssue] = []
    for t in tuples:
        if t.connectivity_state == ConnectivityState.DOWN:
            continue
        if t.config_entry_state != ConfigEntryState.LOADED:
            continue
        if t.all_unavailable:
            continue

        # Entities that genuinely report from the device: the availability
        # telemetry set (``data_entity_ids``), minus anything with no live value.
        eligible = [
            s
            for s in t.entity_states
            if s.entity_id in t.data_entity_ids and s.state not in NO_VALUE_STATES
        ]
        if not eligible:
            continue

        # Read each entity's expected cadence and freshness BEFORE feeding this
        # cycle's observations into the learner; the liveness check reuses both.
        freshest: datetime | None = None  # boot-anchored freshest report
        # Smallest expected cadence among entities with a trusted (populated)
        # cadence; None iff no entity is populated (also gates making any claim).
        min_expected: float | None = None
        expected_by_entity: dict[str, float] = {}
        # Per-entity report time, floored at boot, reused by the liveness check.
        anchored_by_entity: dict[str, datetime] = {}
        for s in eligible:
            ts = s.last_reported or s.last_updated
            # Per-device boot grace (as in Engine 2): an entity whose freshest
            # report predates the restart is aged from boot, not the stale
            # pre-restart timestamp.
            anchored = ts if boot_time is None else max(ts, boot_time)
            anchored_by_entity[s.entity_id] = anchored
            if freshest is None or anchored > freshest:
                freshest = anchored
            expected = max(
                learner.expected_interval(
                    s.entity_id, s.domain, s.attributes.get(ATTR_DEVICE_CLASS)
                ),
                _MIN_EXPECTED_INTERVAL,
            )
            expected_by_entity[s.entity_id] = expected
            if learner.is_populated(s.entity_id):
                if min_expected is None or expected < min_expected:
                    min_expected = expected

        for s in eligible:
            learner.observe(s.entity_id, s.last_reported or s.last_updated)

        # Need at least one entity with a trusted cadence to make a claim.
        if freshest is None or min_expected is None:
            continue

        # Silent only when EVERY eligible entity is overdue against its OWN
        # expected interval — a slower sensor (or one still warming up) that is
        # still on time keeps the device alive, judged against its own cadence,
        # not the fastest sibling's.
        if any(
            (now - anchored_by_entity[s.entity_id]).total_seconds()
            <= multiplier * expected_by_entity[s.entity_id]
            for s in eligible
        ):
            continue

        device_age = (now - freshest).total_seconds()
        # When every report predates the restart, ``freshest`` is floored to
        # ``boot_time`` — the true silence began earlier, so report a lower bound.
        since_is_lower_bound = boot_time is not None and freshest == boot_time
        issues.append(
            VigilIssue(
                kind=IssueKind.SILENT_DEVICE,
                name=t.device_name,
                integration=t.integration_label,
                detail=(
                    f"no update for {int(device_age)}s, expected ~{int(min_expected)}s"
                ),
                since=freshest,
                source="stale",
                device_id=t.device_id,
                config_entry_id=t.config_entry_id,
                domain=t.config_entry_domain,
                since_is_lower_bound=since_is_lower_bound,
            )
        )

    return issues
