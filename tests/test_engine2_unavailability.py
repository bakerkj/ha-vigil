# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.vigil.detection.engines import engine2_unavailability as e2
from custom_components.vigil.detection.engines.engine2_unavailability import (
    detect_unavailability_issues,
)
from custom_components.vigil.models import (
    ConnectivityState,
    DeviceTuple,
    DowntimeRecord,
    IssueKind,
    VigilIssue,
)
from tests.helpers import make_device_tuple

GRACE = timedelta(minutes=15)
BATTERY_MULT = 2.0


def _tuple(
    *,
    device_id: str = "dev1",
    config_entry_id: str | None = "entry1",
    connectivity_state: ConnectivityState = ConnectivityState.DOWN,
    connectivity_source: str = "ping",
    all_unavailable: bool = True,
    any_unavailable: bool | None = None,
    is_battery: bool = False,
    has_data_entities: bool = True,
    offline_since: datetime | None = None,
) -> DeviceTuple:
    return make_device_tuple(
        device_id=device_id,
        config_entry_id=config_entry_id,
        connectivity_state=connectivity_state,
        connectivity_source=connectivity_source,
        all_unavailable=all_unavailable,
        any_unavailable=all_unavailable if any_unavailable is None else any_unavailable,
        is_battery=is_battery,
        data_entity_ids={"sensor.x"} if has_data_entities else set(),
        offline_since=offline_since,
    )


def _detect(
    tuples: list[DeviceTuple],
    *,
    flagged: set[str] | None = None,
    unavailable_since: dict[str, datetime] | None = None,
    lower_bound: set[str] | None = None,
    recorder_resolved: set[str] | None = None,
    now: datetime | None = None,
    boot_time: datetime | None = None,
    battery_mult: float = BATTERY_MULT,
) -> tuple[list[VigilIssue], dict[str, datetime]]:
    lb = lower_bound or set()
    rr = recorder_resolved or set()
    downtime: dict[str, DowntimeRecord] = {
        did: DowntimeRecord(
            since=ts, is_lower_bound=did in lb, recorder_resolved=did in rr
        )
        for did, ts in (unavailable_since or {}).items()
    }
    issues = detect_unavailability_issues(
        tuples,
        flagged_entry_ids=flagged or set(),
        grace_period=GRACE,
        battery_multiplier=battery_mult,
        downtime=downtime,
        now=now or dt_util.utcnow(),
        boot_time=boot_time,
    )
    # Return a datetime view so existing assertions on the tracked "since" hold.
    return issues, {did: rec.since for did, rec in downtime.items()}


def test_no_fire_within_grace() -> None:
    now = dt_util.utcnow()
    since = now - timedelta(minutes=5)
    issues, store = _detect(
        [_tuple()],
        unavailable_since={"dev1": since},
        now=now,
    )
    assert issues == []
    assert store["dev1"] == since


def test_already_offline_fires_immediately_from_offline_since() -> None:
    """A device offline before Vigil started fires at once, not after a fresh grace.

    offline_since (real last_changed) is well past the grace, so even on the
    first observation (empty tracking) it should fire — this is the ESPHome
    'lux node was already down for hours' case.
    """
    now = dt_util.utcnow()
    t = _tuple(
        connectivity_state=ConnectivityState.DOWN,
        offline_since=now - timedelta(hours=3),
    )
    issues, store = _detect([t], now=now)  # note: no pre-seeded unavailable_since
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.DEVICE_OFFLINE_CONFIRMED
    assert issues[0].since == now - timedelta(hours=3)
    assert store["dev1"] == now - timedelta(hours=3)


def test_down_with_partial_unavailable_fires() -> None:
    """Authoritative DOWN + some (not all) entities unavailable counts as offline.

    Mirrors a device kept from all_unavailable by an always-available entity
    (e.g. an ESPHome firmware-update entity) while its ping/status says DOWN.
    """
    now = dt_util.utcnow()
    t = _tuple(
        connectivity_state=ConnectivityState.DOWN,
        all_unavailable=False,
        any_unavailable=True,
        offline_since=now - timedelta(hours=1),
    )
    issues, _ = _detect([t], now=now)
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.DEVICE_OFFLINE_CONFIRMED


def test_down_but_nothing_unavailable_does_not_fire() -> None:
    """DOWN signal but every data entity still reporting → no false positive."""
    now = dt_util.utcnow()
    t = _tuple(
        connectivity_state=ConnectivityState.DOWN,
        all_unavailable=False,
        any_unavailable=False,
        offline_since=None,
    )
    issues, _ = _detect(
        [t], unavailable_since={"dev1": now - timedelta(hours=1)}, now=now
    )
    assert issues == []


def test_signal_only_device_down_fires_without_all_unavailable() -> None:
    """A device with only a connectivity signal (no data entities) reporting DOWN
    is flagged offline even though all_unavailable is False."""
    now = dt_util.utcnow()
    since = now - timedelta(minutes=20)
    issues, _store = _detect(
        [
            _tuple(
                connectivity_state=ConnectivityState.DOWN,
                all_unavailable=False,
                has_data_entities=False,
            )
        ],
        unavailable_since={"dev1": since},
        now=now,
    )
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.DEVICE_OFFLINE_CONFIRMED


def test_signal_only_device_up_does_not_fire() -> None:
    """No data entities + not DOWN must not fire (no false positive)."""
    now = dt_util.utcnow()
    issues, _store = _detect(
        [
            _tuple(
                connectivity_state=ConnectivityState.UP,
                all_unavailable=False,
                has_data_entities=False,
            )
        ],
        unavailable_since={"dev1": now - timedelta(minutes=20)},
        now=now,
    )
    assert issues == []


def test_fires_after_grace_exceeded() -> None:
    now = dt_util.utcnow()
    since = now - timedelta(minutes=20)
    issues, _store = _detect(
        [_tuple(connectivity_state=ConnectivityState.DOWN)],
        unavailable_since={"dev1": since},
        now=now,
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == IssueKind.DEVICE_OFFLINE_CONFIRMED
    assert issue.name == "Device 1"
    assert issue.integration == "Demo"
    assert issue.source == "ping"
    assert issue.since == since
    assert issue.device_id == "dev1"
    assert issue.config_entry_id == "entry1"


def test_unknown_uses_no_signal_kind() -> None:
    now = dt_util.utcnow()
    since = now - timedelta(hours=2)
    issues, _ = _detect(
        [_tuple(connectivity_state=ConnectivityState.UNKNOWN)],
        unavailable_since={"dev1": since},
        now=now,
    )
    assert issues[0].kind == IssueKind.DEVICE_OFFLINE_NO_SIGNAL


def test_first_seen_sets_since_to_now() -> None:
    now = dt_util.utcnow()
    issues, store = _detect([_tuple()], now=now)
    # First observation just records now; not enough elapsed to fire.
    assert issues == []
    assert store["dev1"] == now


@pytest.mark.parametrize(
    ("is_battery", "connectivity_state"),
    [
        # Battery device: doubled grace applies.
        (True, ConnectivityState.UP),
        # Non-battery but UNKNOWN connectivity -> still 2x grace.
        (False, ConnectivityState.UNKNOWN),
    ],
)
def test_double_grace_no_fire_within_doubled_window(
    is_battery: bool, connectivity_state: ConnectivityState
) -> None:
    now = dt_util.utcnow()
    # 20 minutes: past 1x grace (15m) but under 2x grace (30m) -> no fire.
    since = now - timedelta(minutes=20)
    issues, _ = _detect(
        [_tuple(is_battery=is_battery, connectivity_state=connectivity_state)],
        unavailable_since={"dev1": since},
        now=now,
    )
    assert issues == []


def test_battery_fires_past_double_grace() -> None:
    now = dt_util.utcnow()
    since = now - timedelta(minutes=40)
    issues, _ = _detect(
        [_tuple(is_battery=True, connectivity_state=ConnectivityState.UP)],
        unavailable_since={"dev1": since},
        now=now,
    )
    assert len(issues) == 1
    assert issues[0].kind == IssueKind.DEVICE_OFFLINE_NO_SIGNAL


def test_flagged_entry_skipped_and_removed() -> None:
    now = dt_util.utcnow()
    since = now - timedelta(hours=1)
    issues, store = _detect(
        [_tuple(config_entry_id="entry1")],
        flagged={"entry1"},
        unavailable_since={"dev1": since},
        now=now,
    )
    assert issues == []
    assert "dev1" not in store


def test_boot_grace_spares_device_unavailable_only_since_boot() -> None:
    """A non-lower-bound outage whose ``since`` predates the restart is measured
    from boot, not the stale pre-restart timestamp — so a device that simply
    hasn't republished after a reboot is not flagged inside the boot grace."""
    now = dt_util.utcnow()
    boot = now - timedelta(minutes=10)
    # since is a stale pre-restart last_changed (3h old) that survived the reboot.
    issues, _ = _detect(
        [_tuple(connectivity_state=ConnectivityState.DOWN)],
        unavailable_since={"dev1": boot - timedelta(hours=3)},
        now=now,
        boot_time=boot,
    )
    # 10 min since boot < 15 min grace → not flagged, despite the 3h stale since.
    assert issues == []


def test_boot_grace_does_not_spare_recorder_floored_device() -> None:
    """A recorder-resolved floor (lower bound) is exempt from boot grace and fires
    immediately — a device the recorder proves dead for days is never hidden."""
    now = dt_util.utcnow()
    boot = now - timedelta(minutes=10)
    issues, _ = _detect(
        [_tuple(connectivity_state=ConnectivityState.DOWN)],
        unavailable_since={"dev1": now - timedelta(days=7)},
        lower_bound={"dev1"},
        recorder_resolved={"dev1"},
        now=now,
        boot_time=boot,
    )
    assert len(issues) == 1
    assert issues[0].since_is_lower_bound is True


def test_boot_grace_does_not_hide_recorder_resolved_long_dead_device() -> None:
    """A recorder-resolved REAL outage start (not a floor) predating the restart
    must still fire immediately with its true since; a reboot must not hide an
    ESPHome node down ~4 days behind boot grace."""
    now = dt_util.utcnow()
    boot = now - timedelta(minutes=10)
    real_since = now - timedelta(days=4)
    issues, _ = _detect(
        [_tuple(connectivity_state=ConnectivityState.DOWN)],
        unavailable_since={"dev1": real_since},
        recorder_resolved={"dev1"},
        now=now,
        boot_time=boot,
    )
    assert len(issues) == 1
    # Fires with the true 4-day-old since, NOT reset to boot.
    assert issues[0].since == real_since


def test_boot_grace_spares_recorder_resolved_device_healthy_until_just_before_boot() -> (
    None
):
    """A recorder-resolved device whose last good reading was only just before the
    reboot (within its grace) is a slow-to-republish device, not a real outage —
    it must get boot grace and NOT be flagged the moment the startup grace lifts.

    Boot grace applies to a recorder-resolved record unless the recorder proves
    the device was already offline (silent past its grace) *before* the reboot.
    Here it was last seen ~8 min before boot (within the 15 min grace), so it was
    healthy right up to the reboot and merely slow to reconnect.
    """
    now = dt_util.utcnow()
    boot = now - timedelta(minutes=10)
    # Last good reading 8 min before boot — within the 15 min grace, so at boot
    # the device had NOT yet exceeded grace; it was healthy until the reboot.
    issues, _ = _detect(
        [_tuple(connectivity_state=ConnectivityState.DOWN)],
        unavailable_since={"dev1": boot - timedelta(minutes=8)},
        recorder_resolved={"dev1"},
        now=now,
        boot_time=boot,
    )
    # Clock measured from boot (10 min < 15 min grace) → spared, not flagged.
    assert issues == []


def test_boot_grace_fires_once_grace_elapses_measured_from_boot() -> None:
    """Past the boot grace, a still-offline device fires with ``since`` == boot."""
    now = dt_util.utcnow()
    boot = now - timedelta(minutes=40)  # 40 min since boot > 15 min DOWN grace
    issues, _ = _detect(
        [_tuple(connectivity_state=ConnectivityState.DOWN)],
        unavailable_since={"dev1": boot - timedelta(hours=2)},
        now=now,
        boot_time=boot,
    )
    assert len(issues) == 1
    # Duration is measured from boot, not the stale 2h-pre-boot since.
    assert issues[0].since == boot
    assert issues[0].since_is_lower_bound is False


def test_lower_bound_duration_uses_floored_since_as_is() -> None:
    """Engine 2 does not clamp a floored lower bound's displayed duration: the
    seed floors ``since`` to evidence using the same cycle ``now``, so the engine
    just measures ``now - since`` directly. A floored device watched LONGER than
    the lookback window reads exactly that — the lower bound grows, uncapped."""
    now = dt_util.utcnow()
    lookback = timedelta(days=7)
    downtime = {
        "dev1": DowntimeRecord(
            since=now - lookback - timedelta(hours=5),
            is_lower_bound=True,
            recorder_resolved=True,
        )
    }
    issues = detect_unavailability_issues(
        [_tuple(connectivity_state=ConnectivityState.UNKNOWN)],
        flagged_entry_ids=set(),
        grace_period=GRACE,
        battery_multiplier=BATTERY_MULT,
        downtime=downtime,
        now=now,
    )
    dur = issues[0].duration_seconds(now)
    assert dur == (lookback + timedelta(hours=5)).total_seconds()
    assert issues[0].since_is_lower_bound is True


@pytest.mark.parametrize(
    ("connectivity_state", "connectivity_source", "expected_detail", "expected_kind"),
    [
        # A confirmed-down device's detail names the reachability signal, not the
        # (duplicated) duration.
        (
            ConnectivityState.DOWN,
            "connectivity_binary_sensor",
            "Reported down by a connectivity sensor",
            IssueKind.DEVICE_OFFLINE_CONFIRMED,
        ),
        # A MAC-matched router/AP source is rendered with its platform.
        (
            ConnectivityState.DOWN,
            "mac:aruba_instant_ap",
            "Reported down by the router/AP (aruba_instant_ap)",
            IssueKind.DEVICE_OFFLINE_CONFIRMED,
        ),
        # No connectivity signal → the detail makes clear it's inferred, not proven.
        (
            ConnectivityState.UNKNOWN,
            "none",
            "No connectivity signal — inferred from silence",
            IssueKind.DEVICE_OFFLINE_NO_SIGNAL,
        ),
    ],
)
def test_offline_detail_names_the_signal(
    connectivity_state: ConnectivityState,
    connectivity_source: str,
    expected_detail: str,
    expected_kind: IssueKind,
) -> None:
    """The offline detail describes the reachability signal per connectivity source."""
    now = dt_util.utcnow()
    issues, _ = _detect(
        [
            _tuple(
                connectivity_state=connectivity_state,
                connectivity_source=connectivity_source,
            )
        ],
        unavailable_since={"dev1": now - timedelta(hours=2)},
        now=now,
    )
    assert issues[0].kind == expected_kind
    assert issues[0].detail == expected_detail


def test_recovered_device_removed_from_store() -> None:
    now = dt_util.utcnow()
    since = now - timedelta(hours=1)
    issues, store = _detect(
        [_tuple(all_unavailable=False)],
        unavailable_since={"dev1": since},
        now=now,
    )
    assert issues == []
    assert "dev1" not in store


async def test_seed_downtime_total_recorder_failure_leaves_record_unresolved(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TOTAL recorder-read failure reports every device blind with
    run_history_ok=False; a persisted record must be left UNRESOLVED so a later
    healthy cycle re-queries it — not locked with a possibly-stale since."""
    hass.config.components.add("recorder")
    now = dt_util.utcnow()
    since = now - timedelta(days=7)

    async def _blind_total_failure(
        *_a: object, **_k: object
    ) -> tuple[dict[str, datetime], set[str], set[str], bool]:
        return {}, set(), {"dev1"}, False

    downtime = {
        "dev1": DowntimeRecord(
            since=since, is_lower_bound=True, recorder_resolved=False
        )
    }
    monkeypatch.setattr(e2, "async_recorder_last_good", _blind_total_failure)
    await e2.async_seed_downtime(
        hass,
        [_tuple()],
        downtime,
        boot_time=None,
        ha_started=True,
        ha_started_at=None,
        lookback=timedelta(days=7),
        now=now,
    )
    assert downtime["dev1"].recorder_resolved is False  # NOT locked

    async def _blind_read_ok(
        *_a: object, **_k: object
    ) -> tuple[dict[str, datetime], set[str], set[str], bool]:
        return {}, set(), {"dev1"}, True

    # Genuine blindness (read SUCCEEDED, zero rows) does resolve the record.
    monkeypatch.setattr(e2, "async_recorder_last_good", _blind_read_ok)
    await e2.async_seed_downtime(
        hass,
        [_tuple()],
        downtime,
        boot_time=None,
        ha_started=True,
        ha_started_at=None,
        lookback=timedelta(days=7),
        now=now,
    )
    assert downtime["dev1"].recorder_resolved is True


# --- GC only for genuinely-removed devices -----------------------------------


def test_downtime_survives_device_absent_from_tuples_but_in_registry() -> None:
    """A device missing from this cycle's tuples (entities mid-reconnect after a
    restart) but still in the registry keeps its persisted, non-re-derivable
    outage start — it is NOT garbage-collected."""
    now = dt_util.utcnow()
    downtime = {
        "dev1": DowntimeRecord(
            since=now - timedelta(hours=5), is_lower_bound=True, recorder_resolved=False
        )
    }
    detect_unavailability_issues(
        [],  # no tuples this cycle
        flagged_entry_ids=set(),
        grace_period=GRACE,
        battery_multiplier=BATTERY_MULT,
        downtime=downtime,
        now=now,
        known_device_ids={"dev1"},  # still known to the registry
    )
    assert "dev1" in downtime


def test_downtime_gcd_when_device_left_the_registry() -> None:
    now = dt_util.utcnow()
    downtime = {"gone": DowntimeRecord(since=now - timedelta(hours=1))}
    detect_unavailability_issues(
        [],
        flagged_entry_ids=set(),
        grace_period=GRACE,
        battery_multiplier=BATTERY_MULT,
        downtime=downtime,
        now=now,
        known_device_ids=set(),  # no longer in the registry → GC
    )
    assert "gone" not in downtime


# --- recorder seed only reconstructs across-restart devices ------------------


async def test_seed_skips_recorder_for_a_device_seen_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device seen UP this session that later drops is a live-observed outage —
    the seed must NOT hit the recorder for it (that was the per-flap full scan)."""
    hass.config.components.add("recorder")
    now = dt_util.utcnow()
    calls: list[object] = []

    async def _spy(
        *a: object, **k: object
    ) -> tuple[dict[str, datetime], set[str], set[str], bool]:
        calls.append(a)
        return {}, set(), set(), True

    monkeypatch.setattr(e2, "async_recorder_last_good", _spy)
    downtime: dict[str, DowntimeRecord] = {}
    await e2.async_seed_downtime(
        hass,
        [_tuple()],
        downtime,
        boot_time=None,
        ha_started=True,
        ha_started_at=None,
        lookback=timedelta(days=7),
        now=now,
        observed_up={"dev1"},
    )
    assert calls == []  # recorder untouched
    assert "dev1" not in downtime


async def test_seed_reconstructs_a_device_never_seen_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device offline across the restart (never seen up this session) IS still
    reconstructed from the recorder."""
    hass.config.components.add("recorder")
    now = dt_util.utcnow()
    since = now - timedelta(days=3)
    calls: list[object] = []

    async def _spy(
        *a: object, **k: object
    ) -> tuple[dict[str, datetime], set[str], set[str], bool]:
        calls.append(a)
        return {"dev1": since}, set(), set(), True

    monkeypatch.setattr(e2, "async_recorder_last_good", _spy)
    downtime: dict[str, DowntimeRecord] = {}
    await e2.async_seed_downtime(
        hass,
        [_tuple()],
        downtime,
        boot_time=None,
        ha_started=True,
        ha_started_at=None,
        lookback=timedelta(days=7),
        now=now,
        observed_up=frozenset(),
    )
    assert len(calls) == 1
    assert downtime["dev1"].since == since
    assert downtime["dev1"].recorder_resolved is True


def test_downtime_recorder_resolved_round_trips() -> None:
    now = dt_util.utcnow()
    src = {
        "dev1": DowntimeRecord(since=now, is_lower_bound=True, recorder_resolved=True),
        "dev2": DowntimeRecord(
            since=now, is_lower_bound=False, recorder_resolved=False
        ),
    }
    back = e2.deserialize_downtime(e2.serialize_downtime(src))
    assert back["dev1"].recorder_resolved is True
    assert back["dev1"].is_lower_bound is True
    assert back["dev2"].recorder_resolved is False
    # A legacy row without the flag loads unresolved (self-heals via one re-query).
    legacy = e2.deserialize_downtime(
        {"dev3": {"since": now.isoformat(), "is_lower_bound": False}}
    )
    assert legacy["dev3"].recorder_resolved is False
