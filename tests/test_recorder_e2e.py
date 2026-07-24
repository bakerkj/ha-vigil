# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""End-to-end recorder tests: downtime reconstruction against the real recorder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.db_schema import RecorderRuns
from homeassistant.components.recorder.util import (  # type: ignore[attr-defined]
    session_scope,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.vigil.const import (
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_STARTUP_IGNORE_SECONDS,
    DATA_BOOT_TIME,
    DOMAIN,
    RECORDER_LOOKBACK_DAYS,
)
from custom_components.vigil.coordinator import VigilCoordinator
from custom_components.vigil.history.recorder import (
    async_recorder_interval_aggregate,
    select_downtime,
)
from custom_components.vigil.learning.interval_learner import IntervalLearner
from custom_components.vigil.models import IssueKind
from tests.helpers import (
    _make_device,
    _record,
    _recorder_coordinator,
    _signal_only_device,
)


async def test_restore_at_restart_does_not_corrupt_downtime(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device dead >window with a value restored at a 'restart' reports true downtime."""
    now = dt_util.utcnow()

    # A device with a single data sensor and NO connectivity signal (xiaomi_ble
    # plant-sensor shape). Use the loadable 'demo' domain for clean teardown.
    device_id, (eid,) = await _make_device(hass, "plant")

    # --- record real history into the recorder (monotonic forward in time) ---
    # 8 days ago: a genuine reading.
    await _record(
        hass, freezer, eid, "50", at=now - timedelta(days=RECORDER_LOOKBACK_DAYS + 1)
    )
    # 7.5 days ago: the device dies (before the lookback window edge).
    await _record(
        hass,
        freezer,
        eid,
        "unavailable",
        at=now - timedelta(days=RECORDER_LOOKBACK_DAYS, hours=12),
    )
    # 1 hour ago: a RESTART restores the last value, then it goes unavailable
    # again moments later — a lone "good island" between unavailable states.
    await _record(hass, freezer, eid, "48", at=now - timedelta(hours=1))
    await _record(hass, freezer, eid, "unavailable", at=now - timedelta(minutes=59))
    # back to the present: still unavailable.
    await _record(hass, freezer, eid, "unavailable", at=now)

    # Record an HA restart (recorder run) at the restore instant — a real reboot
    # writes this row, and it's how Vigil recognizes the restored value as an
    # artifact rather than a real reading.
    def _add_run() -> None:
        with session_scope(hass=hass) as session:
            session.add(
                RecorderRuns(start=(now - timedelta(hours=1)).replace(tzinfo=None))
            )

    await get_instance(hass).async_add_executor_job(_add_run)
    await async_wait_recording_done(hass)

    # --- run a Vigil cycle against the real recorder ---
    coordinator = await _recorder_coordinator(hass)

    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    assert offline, "dead device should be flagged offline"
    issue = offline[0]
    assert issue.kind is IssueKind.DEVICE_OFFLINE_NO_SIGNAL
    # The restore island at 1h is rejected → floored lower bound, NOT ~1h.
    assert issue.since_is_lower_bound is True
    # The floor must land on the recorder window edge (now - lookback), not some
    # other instant: a floor-to-wrong-edge bug (e.g. flooring to the restart) is
    # caught here, where the loose ">5d" duration check would let it pass.
    assert issue.since is not None
    window_floor = dt_util.utcnow() - timedelta(days=RECORDER_LOOKBACK_DAYS)
    assert abs((issue.since - window_floor).total_seconds()) < 5, (
        f"expected since == window floor {window_floor}, got {issue.since}"
    )
    duration = issue.duration_seconds(dt_util.utcnow())
    assert duration is not None
    assert duration > 5 * 24 * 3600, f"expected >=~7d, got {duration}s (restart leak)"


async def test_interval_learner_recorder_seed_e2e(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The recorder aggregate returns correct per-day MAX gaps + last_good; ingest/flush/reload populates the learner."""
    now = dt_util.utcnow()
    eid = "sensor.cadence_probe"

    # 3 UTC days of history; each day two reports 300s apart (intra-day gap 300),
    # the days one apart (cross-day overnight gap 86100). Distinct values so every
    # report is a recorded state change.
    days = [now - timedelta(days=d) for d in (3, 2, 1)]
    val = 0
    for day in days:
        for offset in (0, 300):
            freezer.move_to(day + timedelta(seconds=offset))
            val += 1
            hass.states.async_set(eid, str(val))
            await async_wait_recording_done(hass)
    freezer.move_to(now)

    learner = IntervalLearner(hass)
    await learner.async_load()

    buckets, last_good = await async_recorder_interval_aggregate(
        hass, [eid], now - timedelta(days=90), now
    )

    # Day 1's first report is the window's first row (null LAG, excluded), so its
    # only gap is the 300s intra-day one. Days 2/3 are dominated by the 86100s
    # cross-day gap. Ordinals must line up with datetime.toordinal().
    assert abs(buckets[(eid, days[0].toordinal())] - 300.0) < 1.0
    assert abs(buckets[(eid, days[1].toordinal())] - 86100.0) < 1.0
    # last_good == the final good report.
    assert (
        abs((last_good[eid] - (days[-1] + timedelta(seconds=300))).total_seconds())
        < 1.0
    )

    # Seed into memory, persist, and reload a fresh learner from the same store.
    learner.ingest(buckets, last_good, now)
    await learner.async_flush(now)
    reloaded = IntervalLearner(hass)
    await reloaded.async_load()
    assert reloaded.is_populated(eid) is True  # 2-day span backdated from the scan
    assert reloaded.learned_interval(eid) is not None
    assert reloaded.watermark() == now


async def test_recorder_gap_spans_unavailable_rows(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A gap is measured good-to-good, not split by an intervening ``unavailable`` row."""
    now = dt_util.utcnow()
    eid = "sensor.flappy_probe"
    day = now - timedelta(days=1)

    # Same UTC day: good @ +0, an unavailable blip @ +100, good again @ +900.
    # Good-to-good gap is 900s; a naive (unfiltered) LAG would see 100s + 800s.
    await _record(hass, freezer, eid, "1", at=day)
    await _record(hass, freezer, eid, "unavailable", at=day + timedelta(seconds=100))
    await _record(hass, freezer, eid, "2", at=day + timedelta(seconds=900))
    freezer.move_to(now)

    buckets, _ = await async_recorder_interval_aggregate(
        hass, [eid], now - timedelta(days=90), now
    )

    # 900s good-to-good — the unavailable row is excluded, not a 800s split max.
    assert abs(buckets[(eid, day.toordinal())] - 900.0) < 1.0


async def test_interval_aggregate_excludes_rows_after_now(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A row timestamped after ``now`` is neither bucketed nor counted as last_good."""
    now = dt_util.utcnow()
    eid = "sensor.future_probe"
    day = now - timedelta(days=1)

    # Two in-window reports 300s apart on `day`.
    await _record(hass, freezer, eid, "1", at=day)
    await _record(hass, freezer, eid, "2", at=day + timedelta(seconds=300))
    # A report timestamped AFTER `now` (a future/clock-skewed row).
    future = now + timedelta(days=1)
    await _record(hass, freezer, eid, "3", at=future)
    freezer.move_to(future + timedelta(seconds=1))

    buckets, last_good = await async_recorder_interval_aggregate(
        hass, [eid], now - timedelta(days=90), now
    )

    # No bucket for the future day, and last_good is the in-window report.
    assert (eid, future.toordinal()) not in buckets
    assert abs((last_good[eid] - (day + timedelta(seconds=300))).total_seconds()) < 1.0


async def test_catchup_overlap_captures_watermark_straddling_gap(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The catch-up seed overlaps its window back one day, so the gap straddling
    the learner's watermark is learned (not dropped as the window's first-row LAG)."""
    now = dt_util.utcnow()
    eid = "sensor.cadence"
    watermark = now - timedelta(hours=6)
    t1 = watermark - timedelta(hours=2)  # before the watermark, inside the overlap
    t2 = watermark + timedelta(hours=2)  # after it; the t1→t2 4h gap straddles it

    await _record(hass, freezer, eid, "1", at=t1)
    await _record(hass, freezer, eid, "2", at=t2)
    freezer.move_to(now)

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    learner = IntervalLearner(hass)
    await learner.async_load()
    # Pretend already caught up to the watermark (empty buckets touch nothing, they
    # just advance the watermark via the public ingest seam).
    learner.ingest({}, {}, watermark)
    coordinator = VigilCoordinator(hass, entry, learner)

    await coordinator._async_seed_learner([eid], now)

    bucket = learner.daily_max(eid).get(t2.toordinal())
    assert bucket is not None, "straddling gap dropped — overlap not applied"
    assert abs(bucket - 4 * 3600) < 5


async def test_signal_only_long_outage_across_restart_fires_as_lower_bound(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A signal-only device down for days across an HA restart fires immediately,
    reporting the true (multi-day) outage as a lower bound — not suppressed for a
    fresh grace nor under-reported from boot."""
    now = dt_util.utcnow()
    device_id, conn_eid = await _signal_only_device(hass, "pinghost4")
    # Transitioned "off" two days ago — well before boot - window.
    dropped = now - timedelta(days=2)
    freezer.move_to(dropped)
    hass.states.async_set(conn_eid, "off")
    freezer.move_to(now)

    coordinator = await _recorder_coordinator(hass)
    coordinator._boot_time = now  # boot ~ now; the 2-day-old drop predates the band

    data = await coordinator._async_update_data()

    record = coordinator._downtime[device_id]
    assert record.is_lower_bound is True
    assert record.recorder_resolved is True  # predates the band → a real transition
    assert abs((record.since - dropped).total_seconds()) < 5

    # It must actually FIRE (proven dead before boot), reporting the true 2-day
    # outage as a lower bound — not be suppressed for a fresh post-boot grace.
    offline = data["devices_offline"]
    assert len(offline) == 1
    assert offline[0].device_id == device_id
    assert offline[0].since_is_lower_bound is True
    since = offline[0].since
    assert since is not None
    assert abs((since - dropped).total_seconds()) < 5


async def test_recorder_blind_downtime_survives_restart(
    recorder_mock: object,
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder-blind device's persisted downtime survives a restart: its true
    (multi-day) outage start is trusted, not reset to boot — the whole point of
    persisting downtime to the store."""

    now = dt_util.utcnow()
    device_id, (eid,) = await _make_device(hass, "blind")
    hass.states.async_set(eid, "unavailable")  # offline

    # Report the recorder as BLIND to this device (zero rows).
    async def _blind(*_a: object, **_k: object) -> object:
        return {}, set(), {device_id}, True

    monkeypatch.setattr(
        "custom_components.vigil.detection.engines.engine2_unavailability."
        "async_recorder_last_good",
        _blind,
    )

    coordinator = await _recorder_coordinator(
        hass, **{CONF_GRACE_PERIOD_MINUTES: 1, CONF_STARTUP_IGNORE_SECONDS: 0}
    )
    learner = coordinator.learner
    coordinator._boot_time = now  # boot ~ now; without trust the since would reset

    # A prior session persisted this device down for 3 days.
    dropped = now - timedelta(days=3)
    await learner.store.async_save_state(
        "downtime", {device_id: {"since": dropped.isoformat(), "is_lower_bound": True}}
    )
    await coordinator.async_load_state()

    data = await coordinator._async_update_data()

    rec = coordinator._downtime[device_id]
    assert rec.recorder_resolved is True  # blind + persisted → trusted
    assert abs((rec.since - dropped).total_seconds()) < 5
    offline = [o for o in data["devices_offline"] if o.device_id == device_id]
    assert len(offline) == 1
    since = offline[0].since
    assert since is not None
    assert abs((since - dropped).total_seconds()) < 5  # true 3-day since, not boot


async def test_run_history_read_failure_marks_record_not_resolved(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed run-history read leaves the record recorder_resolved=False so a later cycle re-queries."""
    now = dt_util.utcnow()

    device_id, (eid,) = await _make_device(hass, "rh")

    # Dead the whole window: a reading before the window edge, then unavailable.
    await _record(
        hass, freezer, eid, "50", at=now - timedelta(days=RECORDER_LOOKBACK_DAYS + 1)
    )
    await _record(
        hass,
        freezer,
        eid,
        "unavailable",
        at=now - timedelta(days=RECORDER_LOOKBACK_DAYS, hours=12),
    )
    await _record(hass, freezer, eid, "unavailable", at=now)

    # Make ONLY the run-history read raise (the get_significant_states history
    # read still succeeds, so the device is still seeded/floored from rows).
    import custom_components.vigil.history.recorder as recorder_mod

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("run history read failed")

    monkeypatch.setattr(recorder_mod, "session_scope", _boom)

    coordinator = await _recorder_coordinator(hass)

    await coordinator._async_update_data()

    record = coordinator._downtime[device_id]
    # Seeded from recorder rows (floored) but NOT locked in, since run history read
    # failed -> re-query next cycle.
    assert record.recorder_resolved is False
    assert record.is_lower_bound is True


async def test_recorder_query_failure_degrades_to_live_grace(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failing recorder history query (DB locked, mid-purge/migration, disk full,
    not-yet-ready) must NOT blank the detection cycle: it degrades to the live
    offline_since + grace path, self-correcting on a later cycle — never propagating
    to mark the whole DataUpdateCoordinator failed."""
    now = dt_util.utcnow()

    # A device whose two data sensors have been unavailable for 2h (past grace).
    device_id, eids = await _make_device(hass, "plant", 2)
    freezer.move_to(now - timedelta(hours=2))
    for eid in eids:
        hass.states.async_set(eid, "unavailable")
    freezer.move_to(now)

    coordinator = await _recorder_coordinator(
        hass,
        **{CONF_GRACE_PERIOD_MINUTES: 15, CONF_BATTERY_GRACE_MULTIPLIER: 2.0},
    )

    # The recorder read blows up mid-cycle.
    with patch(
        "custom_components.vigil.history.recorder.get_significant_states",
        side_effect=RuntimeError("database is locked"),
    ):
        data = await coordinator._async_update_data()

    # The cycle SURVIVED (did not raise / blank) and produced a normal payload.
    assert "devices_offline" in data and "counts" in data
    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    # And the device is still caught — via the live offline_since path (2h > 2x
    # grace for UNKNOWN connectivity), NOT a recorder floor.
    assert offline, "dead device must still be flagged despite the recorder failure"
    assert offline[0].since_is_lower_bound is False  # live value, not a floor


async def test_signal_only_offline_seeded_from_signal_transition(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A signal-only DOWN device is seeded from its own transition (exact), not a window floor."""
    now = dt_util.utcnow()
    device_id, conn_eid = await _signal_only_device(hass, "pinghost")
    # Went DOWN 2h ago and stayed down — the only entity on the device.
    down_since = now - timedelta(hours=2)
    await _record(hass, freezer, conn_eid, "off", at=down_since)
    freezer.move_to(now)

    coordinator = await _recorder_coordinator(hass)

    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    assert offline, "signal-only DOWN device should be flagged past grace"
    issue = offline[0]
    # Seeded from the real signal transition (~2h), NOT a lower-bound window floor.
    assert issue.since_is_lower_bound is False
    assert issue.since is not None
    assert abs((issue.since - down_since).total_seconds()) < 5


async def test_signal_only_boot_artifact_seeds_lower_bound_not_boot_time(
    recorder_mock: object,
    hass: HomeAssistant,
) -> None:
    """A signal-only device up-already-DOWN at boot seeds an unlocked lower bound from boot."""
    now = dt_util.utcnow()
    device_id, conn_eid = await _signal_only_device(hass, "pinghost")
    # Came up "off" at ~boot: last_changed is the restart instant, not the true
    # (older) transition.
    hass.states.async_set(conn_eid, "off")

    coordinator = await _recorder_coordinator(hass)
    # Simulate having been set up at boot: the signal's last_changed (~now) falls in
    # the restart-artifact window of this boot instant.
    coordinator._boot_time = now

    await coordinator._async_update_data()

    record = coordinator._downtime[device_id]
    assert record.is_lower_bound is True
    assert record.recorder_resolved is False
    assert record.since == now  # seeded from boot, not the boot-reset last_changed


async def test_signal_only_drop_well_after_boot_is_trusted_not_artifact(
    recorder_mock: object,
    hass: HomeAssistant,
) -> None:
    """A signal-only drop long after boot is trusted as a real transition, not folded to boot."""
    now = dt_util.utcnow()
    boot = now - timedelta(days=3)  # HA booted 3 days ago; the drop is recent
    device_id, conn_eid = await _signal_only_device(hass, "pinghost2")
    # Dropped just now — its last_changed is ~now, three days past boot.
    hass.states.async_set(conn_eid, "off")

    coordinator = await _recorder_coordinator(hass)
    coordinator._boot_time = (
        boot  # boot is far in the past → the drop isn't an artifact
    )

    await coordinator._async_update_data()

    record = coordinator._downtime[device_id]
    # Trusted as the real (recent) transition, NOT folded back to boot 3 days ago.
    assert record.is_lower_bound is False
    assert record.recorder_resolved is True
    assert abs((record.since - now).total_seconds()) < 5


async def test_signal_only_off_during_slow_startup_is_artifact(
    recorder_mock: object,
    hass: HomeAssistant,
) -> None:
    """A device reporting 'off' during a slow startup (past boot+window) is still a boot artifact."""
    now = dt_util.utcnow()
    boot = now - timedelta(minutes=8)  # booted 8 min ago (past a 5-min window)
    device_id, conn_eid = await _signal_only_device(hass, "pinghost3")
    # A slow integration only now published the device as "off" (last_changed ~now).
    hass.states.async_set(conn_eid, "off")

    coordinator = await _recorder_coordinator(hass)
    coordinator._boot_time = boot
    # HA finished starting essentially now (an 8-minute boot) — the "off" arrived
    # within that startup, so it's an artifact even though it's > boot + 5 min.
    coordinator._ha_started = True
    coordinator._ha_started_at = now

    await coordinator._async_update_data()

    record = coordinator._downtime[device_id]
    assert record.is_lower_bound is True
    assert record.recorder_resolved is False
    assert record.since == boot  # anchored to boot, not the late startup report


# ---------------------------------------------------------------------------
# Boot grace: an "unavailable only since shortly after boot" device (MQTT /
# Frigate / ESPresence reconnecting slower than the global startup grace) must not
# be flagged with a bogus duration while inside the per-device boot grace. A
# recorder-proven-dead (lower-bound) device is exempt and still fires immediately.
# ---------------------------------------------------------------------------
def test_frigate_duration_comes_from_offline_since_not_recorder() -> None:
    """Pin the source of the bogus '3h 24m'.

    With intact recorder history (continuous good values up to the restart, then
    a post-restart 'unavailable'), ``select_downtime`` returns the LAST good
    value (~just before the restart) — a short outage, NOT 3h24m — and does not
    floor. So the live '3h 24m' must come from the live-state fallback
    ``offline_since`` (= the first earlier blip), used by Engine 2 whenever the
    recorder seed didn't resolve the device that cycle (recorder not loaded yet).
    """
    day = datetime(2026, 6, 26, 0, 0, 0, tzinfo=UTC)
    boot = day + timedelta(hours=22)
    now = boot + timedelta(minutes=13)
    start = now - timedelta(days=7)

    def S(ts: datetime, val: str) -> State:
        s = State("sensor.frigate_camera_a_bytes_rate", val)
        s.last_changed = ts
        return s

    seq: list[State | dict[str, object]] = [
        S(day + timedelta(hours=16), "1000"),
        S(day + timedelta(hours=18), "1000"),
        S(day + timedelta(hours=18, minutes=49), "unavailable"),
        S(day + timedelta(hours=18, minutes=49, seconds=20), "1000"),
        S(day + timedelta(hours=19, minutes=33), "unavailable"),
        S(day + timedelta(hours=19, minutes=33, seconds=20), "1000"),
        S(day + timedelta(hours=19, minutes=59), "unavailable"),
        S(day + timedelta(hours=19, minutes=59, seconds=20), "1000"),
    ]
    t = day + timedelta(hours=20)
    while t < boot - timedelta(minutes=2):
        seq.append(S(t, "1000"))
        t += timedelta(minutes=5)
    seq.append(S(boot, "unavailable"))  # post-restart write

    hist = {"sensor.frigate_camera_a_bytes_rate": seq}
    e2d = {"sensor.frigate_camera_a_bytes_rate": "dev_frigate"}
    window = timedelta(seconds=300)

    res, floored, _blind = select_downtime(hist, e2d, boot, [boot], window, start)
    recorder_dur_h = (now - res["dev_frigate"]).total_seconds() / 3600
    # Intact history -> last good ~just before restart -> < 1h, NOT 3.4h.
    assert "dev_frigate" not in floored
    assert recorder_dur_h < 1.0, recorder_dur_h

    # The 3h24m matches offline_since == the FIRST blip at 18:49.
    first_blip = day + timedelta(hours=18, minutes=49)
    offline_since_dur_h = (now - first_blip).total_seconds() / 3600
    assert round(offline_since_dur_h, 1) == 3.4


async def test_healthy_device_not_flagged_during_boot_grace(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """End-to-end, matching prod: a device unavailable only since the restart, no
    recorder loaded yet, is NOT flagged while inside the per-device boot grace.
    Grace is measured from the boot anchor, not any stale pre-restart
    ``offline_since``.
    """
    now = dt_util.utcnow()
    boot_time = now - timedelta(minutes=10)  # restarted 10 min ago

    # No recorder component → recorder seed is skipped, exactly like the first
    # post-grace cycle in prod before the recorder finishes loading.
    assert "recorder" not in hass.config.components

    # Vigil set up during HA startup so it records the boot anchor like prod.
    hass.set_state(CoreState.starting)
    hass.data.pop(DATA_BOOT_TIME, None)

    device_id, (eid,) = await _make_device(hass, "frigate_camera_a")
    # The sensor went unavailable at boot (last_changed == boot) and has not
    # republished since.
    freezer.move_to(boot_time)
    hass.states.async_set(eid, "unavailable")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            CONF_GRACE_PERIOD_MINUTES: 15,
            CONF_STARTUP_IGNORE_SECONDS: 300,
        },
    )
    entry.add_to_hass(hass)
    learner = IntervalLearner(hass)
    await learner.async_load()
    coordinator = VigilCoordinator(hass, entry, learner)
    assert coordinator._boot_time == boot_time

    # HA finishes starting; the global startup grace lifts.
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    # First cycle ~10 min post-boot; device still unavailable (not republished).
    freezer.move_to(now)
    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    assert offline == [], (
        "device unavailable only since boot (no good report post-restart) must "
        "be inside the per-device boot grace, not flagged"
    )
