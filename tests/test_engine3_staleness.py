# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.vigil.const import heuristic_interval
from custom_components.vigil.detection.engines.engine3_staleness import (
    detect_staleness_issues,
)
from custom_components.vigil.learning.interval_learner import IntervalLearner
from custom_components.vigil.models import (
    ConnectivityState,
    DeviceTuple,
    IssueKind,
)
from tests.helpers import make_device_tuple, seed_learner

MULTIPLIER = 3.0


def _prelearn_daily_max(
    learner: IntervalLearner, entity_id: str, *, gap_seconds: float, days: int
) -> None:
    """Seed the learner with a `gap_seconds` longest-gap on each of `days` days."""
    seed_learner(learner, entity_id, gap_seconds=gap_seconds, days=days)


async def test_real_learner_tolerates_normal_quiet_but_flags_dead(
    hass: HomeAssistant,
) -> None:
    """Real learner + Engine 3: an ~8h learned gap tolerates a 43-min quiet but
    flags once silent past 8h * multiplier."""
    now = dt_util.utcnow()
    eid = "binary_sensor.room_motion"
    overnight = 8 * 3600.0

    not_stale = IntervalLearner(hass)
    _prelearn_daily_max(not_stale, eid, gap_seconds=overnight, days=5)
    assert not_stale.learned_interval(eid) == overnight
    quiet = _tuple(
        [_state(eid, "off", age_seconds=43 * 60, device_class="motion", now=now)]
    )
    assert (
        detect_staleness_issues(
            [quiet], learner=not_stale, multiplier=MULTIPLIER, now=now
        )
        == []
    )

    # Fresh learner (the call above observed and mutated the first one).
    dead_learner = IntervalLearner(hass)
    _prelearn_daily_max(dead_learner, eid, gap_seconds=overnight, days=5)
    dead = _tuple(
        [_state(eid, "off", age_seconds=26 * 3600, device_class="motion", now=now)]
    )
    issues = detect_staleness_issues(
        [dead], learner=dead_learner, multiplier=MULTIPLIER, now=now
    )
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.SILENT_DEVICE


class FakeLearner:
    """Minimal stand-in for IntervalLearner."""

    def __init__(self, *, populated: bool, expected: float) -> None:
        self._populated = populated
        self._expected = expected
        self.observed: list[tuple[str, datetime]] = []

    def is_populated(self, entity_id: str) -> bool:
        return self._populated

    def expected_interval(
        self, entity_id: str, domain: str, device_class: str | None
    ) -> float:
        return self._expected

    def observe(self, entity_id: str, timestamp: datetime) -> None:
        self.observed.append((entity_id, timestamp))


class PerEntityLearner:
    """Learner stub with a distinct cadence per entity (for the flap test)."""

    def __init__(self, expected: dict[str, float]) -> None:
        self._expected = expected
        self.observed: list[tuple[str, datetime]] = []

    def is_populated(self, entity_id: str) -> bool:
        return entity_id in self._expected

    def expected_interval(
        self, entity_id: str, domain: str, device_class: str | None
    ) -> float:
        # Learned cadence when populated, else the domain/device_class heuristic.
        if entity_id in self._expected:
            return self._expected[entity_id]
        return heuristic_interval(domain, device_class)

    def observe(self, entity_id: str, timestamp: datetime) -> None:
        self.observed.append((entity_id, timestamp))


def _state(
    entity_id: str = "sensor.x",
    state: str = "23",
    *,
    age_seconds: float,
    device_class: str | None = "temperature",
    now: datetime,
) -> State:
    """A state whose last report (last_reported) is age_seconds in the past."""
    attrs: dict[str, str] = {}
    if device_class is not None:
        attrs["device_class"] = device_class
    last = now - timedelta(seconds=age_seconds)
    return State(
        entity_id,
        state,
        attrs,
        last_changed=last,
        last_updated=last,
        last_reported=last,
    )


def _tuple(
    states: list[State],
    *,
    connectivity_state: ConnectivityState = ConnectivityState.UP,
    config_entry_state: ConfigEntryState | None = ConfigEntryState.LOADED,
    all_unavailable: bool = False,
    signal_entity_ids: set[str] | None = None,
    data_entity_ids: set[str] | None = None,
) -> DeviceTuple:
    signal_ids = signal_entity_ids or set()
    # Mirror inputs.build_device_tuples: data_entity_ids defaults to every
    # non-signal entity; tests override it to model annotation exclusion.
    if data_entity_ids is None:
        data_entity_ids = {s.entity_id for s in states if s.entity_id not in signal_ids}
    return make_device_tuple(
        config_entry_state=config_entry_state,
        connectivity_state=connectivity_state,
        entity_states=states,
        all_unavailable=all_unavailable,
        any_unavailable=all_unavailable,
        signal_entity_ids=signal_ids,
        data_entity_ids=data_entity_ids,
    )


def test_fires_when_device_silent() -> None:
    """A device whose freshest report is past the threshold is flagged."""
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)
    s = _state(age_seconds=2000, now=now)  # 2000 > 3 * 600
    issues = detect_staleness_issues(
        [_tuple([s])], learner=learner, multiplier=MULTIPLIER, now=now
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == IssueKind.SILENT_DEVICE
    assert issue.name == "Device 1"
    assert issue.source == "stale"
    assert issue.since == s.last_reported
    assert issue.device_id == "dev1"
    assert "expected ~600s" in issue.detail
    assert "no update for 2000s" in issue.detail
    assert learner.observed == [("sensor.x", s.last_reported)]


def test_fresh_entity_keeps_device_alive() -> None:
    """A stale unchanging entity must not flag a device with fresh activity."""
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)
    stale_switch = _state(
        "switch.printer", "on", age_seconds=5000, device_class=None, now=now
    )
    fresh_sensor = _state("sensor.power", "42", age_seconds=30, now=now)
    issues = detect_staleness_issues(
        [_tuple([stale_switch, fresh_sensor])],
        learner=learner,
        multiplier=MULTIPLIER,
        now=now,
    )
    assert issues == []
    # Both entities are still observed so the learner keeps learning.
    assert len(learner.observed) == 2


def test_fires_only_when_all_entities_old() -> None:
    """When even the freshest entity is past threshold, the device is silent."""
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)
    older = _state("sensor.a", "1", age_seconds=5000, now=now)
    freshest = _state("sensor.b", "2", age_seconds=2200, now=now)  # still > 1800
    issues = detect_staleness_issues(
        [_tuple([older, freshest])],
        learner=learner,
        multiplier=MULTIPLIER,
        now=now,
    )
    assert len(issues) == 1
    # 'since' reflects the freshest report, not the oldest.
    assert issues[0].since == freshest.last_reported
    assert "no update for 2200s" in issues[0].detail


def test_signal_entity_never_flagged_stale() -> None:
    """A connectivity/status signal entity is never reported as a silent device."""
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)
    signal = _state(
        "binary_sensor.ping", "on", age_seconds=100000, device_class=None, now=now
    )
    issues = detect_staleness_issues(
        [_tuple([signal], signal_entity_ids={"binary_sensor.ping"})],
        learner=learner,
        multiplier=MULTIPLIER,
        now=now,
    )
    assert issues == []
    assert learner.observed == []


def test_annotation_entity_does_not_mask_silent_device() -> None:
    """A self-updating annotation entity (excluded from data_entity_ids) must not
    keep a silent device alive."""
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)
    dead_sensor = _state("sensor.temperature", "21", age_seconds=5000, now=now)
    # Annotation entity reporting 30s ago — in entity_states but NOT telemetry.
    fresh_note = _state(
        "sensor.battery_note", "OK", age_seconds=30, device_class=None, now=now
    )
    issues = detect_staleness_issues(
        [
            _tuple(
                [dead_sensor, fresh_note],
                data_entity_ids={"sensor.temperature"},
            )
        ],
        learner=learner,
        multiplier=MULTIPLIER,
        now=now,
    )
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.SILENT_DEVICE
    assert issues[0].since == dead_sensor.last_reported


def test_boot_grace_suppresses_pre_restart_then_flags() -> None:
    """An entity whose only report predates the restart is measured from boot: not
    flagged until enough time has passed SINCE BOOT for it to have reported."""
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)  # threshold = 3*600 = 1800s
    stale = _state(age_seconds=7200, now=now)  # reported 2h ago (before either boot)

    # Booted 100s ago → age measured from boot is 100s (< 1800) → not flagged.
    recent_boot = now - timedelta(seconds=100)
    assert (
        detect_staleness_issues(
            [_tuple([stale])],
            learner=learner,
            multiplier=MULTIPLIER,
            now=now,
            boot_time=recent_boot,
        )
        == []
    )

    # Booted 3000s ago → age from boot is 3000s (> 1800) → flagged.
    old_boot = now - timedelta(seconds=3000)
    issues = detect_staleness_issues(
        [_tuple([stale])],
        learner=learner,
        multiplier=MULTIPLIER,
        now=now,
        boot_time=old_boot,
    )
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.SILENT_DEVICE
    # The report predates boot, so the outage start is a lower bound (≥), not exact.
    assert issues[0].since_is_lower_bound is True
    assert issues[0].since == old_boot


def test_zero_expected_cadence_does_not_flag_recent_report() -> None:
    """A degenerate expected cadence of 0 must not flag a just-reported device (the
    floor keeps multiplier * expected positive)."""
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=0.0)
    s = _state(age_seconds=2, now=now)  # reported 2s ago
    assert (
        detect_staleness_issues(
            [_tuple([s])], learner=learner, multiplier=MULTIPLIER, now=now
        )
        == []
    )


def test_no_fire_when_not_populated() -> None:
    now = dt_util.utcnow()
    learner = FakeLearner(populated=False, expected=600.0)
    s = _state(age_seconds=100000, now=now)  # very old
    issues = detect_staleness_issues(
        [_tuple([s])], learner=learner, multiplier=MULTIPLIER, now=now
    )
    assert issues == []
    # observe still called even though nothing is trusted yet
    assert learner.observed == [("sensor.x", s.last_reported)]


@pytest.mark.parametrize(
    ("age", "tuple_kwargs", "expected_observed"),
    [
        # Age below the staleness threshold (1000 < 1800).
        (1000, {}, None),
        # Config entry not loaded — Engine 1's job.
        (5000, {"config_entry_state": ConfigEntryState.SETUP_RETRY}, None),
        # All entities unavailable — Engine 2's job.
        (5000, {"all_unavailable": True}, None),
        # Hard DOWN — Engine 2's job; the device isn't even observed.
        (5000, {"connectivity_state": ConnectivityState.DOWN}, []),
    ],
)
def test_no_fire_when_populated(
    age: int,
    tuple_kwargs: dict[str, Any],
    expected_observed: list[object] | None,
) -> None:
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)
    issues = detect_staleness_issues(
        [_tuple([_state(age_seconds=age, now=now)], **tuple_kwargs)],
        learner=learner,
        multiplier=MULTIPLIER,
        now=now,
    )
    assert issues == []
    if expected_observed is not None:
        assert learner.observed == expected_observed


def test_fires_when_connectivity_unknown() -> None:
    """A silent device with UNKNOWN connectivity (BLE/cloud) IS flagged."""
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)
    # A plant moisture sensor stuck at "0", last reported well past its cadence.
    stuck = _state("sensor.plant_moisture", "0", age_seconds=5000, now=now)
    issues = detect_staleness_issues(
        [_tuple([stuck], connectivity_state=ConnectivityState.UNKNOWN)],
        learner=learner,
        multiplier=MULTIPLIER,
        now=now,
    )
    assert len(issues) == 1
    assert issues[0].kind is IssueKind.SILENT_DEVICE


def test_fast_diagnostic_gap_does_not_flap_device() -> None:
    """A brief gap in one fast diagnostic must not flag the whole device when a
    slow sibling is still within its own cadence."""
    now = dt_util.utcnow()
    learner = PerEntityLearner({"sensor.rssi": 30.0, "sensor.temp": 600.0})
    rssi = _state("sensor.rssi", "42", age_seconds=120, device_class=None, now=now)
    temp = _state("sensor.temp", "21", age_seconds=300, now=now)  # within 3*600
    issues = detect_staleness_issues(
        [_tuple([rssi, temp])], learner=learner, multiplier=MULTIPLIER, now=now
    )
    assert issues == []


def test_unpopulated_recent_entity_keeps_device_alive() -> None:
    """A populated-but-overdue entity plus an unpopulated entity that just reported
    keeps the device alive via the freshest-within-fastest fallback."""
    now = dt_util.utcnow()
    learner = PerEntityLearner({"sensor.temp": 600.0})  # only temp has a cadence
    temp = _state("sensor.temp", "21", age_seconds=5000, now=now)  # overdue (>1800)
    rssi = _state(
        "sensor.rssi", "42", age_seconds=10, device_class=None, now=now
    )  # fresh, no learned cadence
    issues = detect_staleness_issues(
        [_tuple([temp, rssi])], learner=learner, multiplier=MULTIPLIER, now=now
    )
    assert issues == []


def test_slow_unpopulated_entity_judged_against_own_cadence_not_fastest() -> None:
    """A slow entity on time per its own heuristic keeps the device alive even when
    a fast POPULATED sibling has died."""
    now = dt_util.utcnow()
    learner = PerEntityLearner({"sensor.fast": 60.0})  # fast, learned; dead below
    fast = _state("sensor.fast", age_seconds=5000, now=now)  # overdue (>> 3*60)
    # Slow diagnostic, no learned cadence → heuristic 600s (temperature). Reporting
    # 300s ago: within 3*600 of ITS OWN cadence, but > 3*60 of the fast sibling's.
    slow = _state("sensor.slow", age_seconds=300, device_class="temperature", now=now)

    issues = detect_staleness_issues(
        [_tuple([fast, slow])], learner=learner, multiplier=MULTIPLIER, now=now
    )
    assert issues == []  # kept alive by the slow entity's own on-time report


def test_silent_when_every_entity_overdue_per_own_cadence() -> None:
    """The device is flagged only when EVERY entity is overdue vs its cadence."""
    now = dt_util.utcnow()
    learner = PerEntityLearner({"sensor.rssi": 30.0, "sensor.temp": 600.0})
    rssi = _state("sensor.rssi", "42", age_seconds=5000, device_class=None, now=now)
    temp = _state("sensor.temp", "21", age_seconds=5000, now=now)  # > 3*600
    issues = detect_staleness_issues(
        [_tuple([rssi, temp])], learner=learner, multiplier=MULTIPLIER, now=now
    )
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.SILENT_DEVICE


def test_unavailable_unknown_states_skipped() -> None:
    now = dt_util.utcnow()
    learner = FakeLearner(populated=True, expected=600.0)
    s_unavail = _state("sensor.a", "unavailable", age_seconds=5000, now=now)
    s_unknown = _state("sensor.b", "unknown", age_seconds=5000, now=now)
    issues = detect_staleness_issues(
        [_tuple([s_unavail, s_unknown])],
        learner=learner,
        multiplier=MULTIPLIER,
        now=now,
    )
    assert issues == []
    assert learner.observed == []
