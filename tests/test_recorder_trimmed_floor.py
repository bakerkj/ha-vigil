# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Floor-to-evidence under NON-UNIFORM recorder retention (recorder-downsampling).

The user's recorder trims DIFFERENT entities at DIFFERENT horizons. Vigil's
downtime floor must therefore floor a dead-but-unresolved device to the EDGE OF
THE EVIDENCE the recorder actually has — ``max(start, oldest recorded row)`` —
rather than blindly to the lookback window. STEP 1 verified the recorder edge
behavior empirically:

  * FULL retention (history before ``start``): get_significant_states synthesizes
    a start-time edge state whose ``last_changed`` is clamped exactly to ``start``
    -> oldest row == start -> floor reads a clean "≥ window".
  * TRIMMED (oldest row inside the window, nothing before ``start``): NO synthetic
    edge state; the oldest row is the real trim edge inside the window -> floor
    reads an HONEST "≥ R" (R < window), not a phantom "≥ window".

This suite drives the REAL recorder (recorder_mock + freezer), like
test_recorder_e2e.py, and exercises: full-retention, trimmed, blind, drift, and
the configurable lookback. Each boundary case was revert-verified.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.vigil.const import (
    CONF_RECORDER_LOOKBACK_DAYS,
    RECORDER_LOOKBACK_DAYS,
)
from custom_components.vigil.models import IssueKind
from tests.helpers import _make_device, _record, _recorder_coordinator


# ---------------------------------------------------------------------------
# FULL retention: history before the window -> floor == exactly the window edge,
# reading a clean "7d 0h", NEVER "6d 23h".
# ---------------------------------------------------------------------------
async def test_full_retention_dead_device_floors_to_clean_window(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    now = dt_util.utcnow()
    device_id, (eid,) = await _make_device(hass, "fullret")

    # A good reading BEFORE the window edge, then dead the whole window.
    await _record(
        hass, freezer, eid, "50", at=now - timedelta(days=RECORDER_LOOKBACK_DAYS + 2)
    )
    await _record(
        hass,
        freezer,
        eid,
        "unavailable",
        at=now - timedelta(days=RECORDER_LOOKBACK_DAYS + 1),
    )
    await _record(hass, freezer, eid, "unavailable", at=now)

    coordinator = await _recorder_coordinator(hass)
    freezer.move_to(now)
    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    assert offline, "full-retention dead device must be flagged"
    issue = offline[0]
    assert issue.kind is IssueKind.DEVICE_OFFLINE_NO_SIGNAL
    assert issue.since_is_lower_bound is True
    assert issue.since is not None
    window_floor = now - timedelta(days=RECORDER_LOOKBACK_DAYS)
    # Floors to EXACTLY the window edge (the synthetic start-time edge state),
    # within a couple seconds of recorder write jitter.
    assert abs((issue.since - window_floor).total_seconds()) < 3, (
        f"expected since == window floor {window_floor}, got {issue.since}"
    )
    # And reads a clean "7d 0h", never "6d 23h" (the sub-second epsilon is gone
    # because start is derived from the threaded cycle ``now``).
    dur = issue.duration_seconds(now)
    assert dur is not None
    from custom_components.vigil.models import humanize_duration

    assert humanize_duration(dur).startswith("7d"), humanize_duration(dur)


# ---------------------------------------------------------------------------
# TRIMMED: oldest row at now-R (R < window) -> floor to now-R -> honest "≥ R",
# NOT "≥ window". This is the core non-uniform-retention requirement.
# ---------------------------------------------------------------------------
async def test_trimmed_dead_device_floors_to_trim_edge_not_window(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    now = dt_util.utcnow()
    device_id, (eid,) = await _make_device(hass, "trimmed")

    # The OLDEST recorder row is INSIDE the window: 3 days ago (this datapoint was
    # trimmed at a 3-day horizon). Nothing exists before that. It is dead from the
    # trim edge onward (no good value in the window).
    r_days = 3
    await _record(hass, freezer, eid, "unavailable", at=now - timedelta(days=r_days))
    await _record(hass, freezer, eid, "unavailable", at=now)

    coordinator = await _recorder_coordinator(hass)
    freezer.move_to(now)
    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    assert offline, "trimmed dead device must still be flagged"
    issue = offline[0]
    assert issue.since_is_lower_bound is True
    assert issue.since is not None
    trim_edge = now - timedelta(days=r_days)
    window_floor = now - timedelta(days=RECORDER_LOOKBACK_DAYS)
    # Floors to the TRIM EDGE (~3d), not the window (~7d): absence of older data
    # is not evidence of a longer outage.
    assert abs((issue.since - trim_edge).total_seconds()) < 3, (
        f"expected since == trim edge {trim_edge}, got {issue.since}"
    )
    # Explicitly NOT the window floor — a trimmed device must not be over-stated
    # to the full window.
    assert issue.since > window_floor + timedelta(days=1), (
        f"trimmed device over-stated to the window: since={issue.since}"
    )
    dur = issue.duration_seconds(now)
    assert dur is not None
    # ~3 days, clearly under the 7-day window.
    assert 2.5 * 24 * 3600 < dur < 4 * 24 * 3600, f"expected ~3d, got {dur}s"


# ---------------------------------------------------------------------------
# BLIND: zero rows for every data entity -> NOT floored, NOT flagged offline >=7d
# (unchanged behavior; this guards the floor-to-evidence change didn't regress it).
# A recorder-EXCLUDED sensor (recorder.yaml drops it) genuinely yields zero rows.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "recorder_config",
    [{"exclude": {"entity_globs": ["sensor.*blindfloor*"]}}],
)
async def test_recorder_blind_device_still_not_floored(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """select_downtime must distinguish (a) a device with recorder rows but no
    good value (genuinely dead → floor to ">= window" is correct) from (b) a
    device whose data entities have ZERO recorder rows (recorder-blind/excluded →
    no evidence of any outage, so flooring would stamp a phantom ">= window" on a
    live device). This models (b): a recorder-EXCLUDED sensor, briefly ``unknown``
    post-restart. It must NOT be floored / flagged on the first cycle."""
    now = dt_util.utcnow()
    hub = MockConfigEntry(domain="demo", title="Blind Hub")
    hub.add_to_hass(hass)
    hub.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id, identifiers={("demo", "blindfloor")}
    )
    # The device's only data entity is recorder-EXCLUDED -> zero rows ever.
    sensor = ent_reg.async_get_or_create(
        "sensor", "demo", "blindfloor_1", device_id=device.id
    )
    eid = sensor.entity_id
    assert "blindfloor" in eid

    # Control entity so the recorder is provably running (writes rows for it),
    # proving the device's own entity has no rows only because it is excluded.
    other = ent_reg.async_get_or_create("sensor", "demo", "blind_ctrl", device_id=None)
    freezer.move_to(now)
    hass.states.async_set(other.entity_id, "ok")
    # Briefly unavailable post-restart; it would recover within seconds.
    hass.states.async_set(eid, "unavailable")
    await async_wait_recording_done(hass)

    coordinator = await _recorder_coordinator(hass)
    freezer.move_to(now)
    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device.id]
    floored = [i for i in offline if i.since_is_lower_bound]
    assert not floored, (
        "recorder-blind device (zero rows) must NOT be floored to a lower bound; "
        f"got {[(i.kind, i.detail) for i in floored]}"
    )


# ---------------------------------------------------------------------------
# DRIFT: on a later cycle the persisted floored record reads slightly MORE than
# the window (still down). That extra is correct and must be preserved.
# ---------------------------------------------------------------------------
async def test_floored_record_drift_past_window_preserved(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    now = dt_util.utcnow()
    device_id, (eid,) = await _make_device(hass, "drift")

    await _record(
        hass, freezer, eid, "50", at=now - timedelta(days=RECORDER_LOOKBACK_DAYS + 2)
    )
    await _record(
        hass,
        freezer,
        eid,
        "unavailable",
        at=now - timedelta(days=RECORDER_LOOKBACK_DAYS + 1),
    )
    await _record(hass, freezer, eid, "unavailable", at=now)

    coordinator = await _recorder_coordinator(hass)
    freezer.move_to(now)
    await coordinator._async_update_data()
    first_since = coordinator._downtime[device_id].since

    # Advance the clock 6 hours and re-run. The record is recorder_resolved, so it
    # is NOT re-queried; its since stays fixed while now advances -> the duration
    # drifts PAST the window. The persisted floored since must be preserved.
    later = now + timedelta(hours=6)
    freezer.move_to(later)
    data = await coordinator._async_update_data()

    assert coordinator._downtime[device_id].since == first_since, (
        "floored record's since must be preserved across cycles (no re-floor)"
    )
    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    assert offline
    dur = offline[0].duration_seconds(later)
    assert dur is not None
    # Now reads MORE than the window (7d + ~6h) — drift preserved, not capped.
    assert dur > (RECORDER_LOOKBACK_DAYS * 24 + 5) * 3600, (
        f"expected drift past window, got {dur}s"
    )


# ---------------------------------------------------------------------------
# LOOKBACK TUNABLE: a larger configured lookback reconstructs a TRUE since for a
# device whose good value is older than the default 7d but within the new window;
# and the floor only applies beyond available data.
# ---------------------------------------------------------------------------
async def test_larger_lookback_reconstructs_true_since_beyond_default(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    now = dt_util.utcnow()
    device_id, (eid,) = await _make_device(hass, "longlook")

    # Last good value 10 days ago — OUTSIDE the default 7d window, INSIDE a
    # configured 30d window. Then dead.
    good_at = now - timedelta(days=10)
    await _record(hass, freezer, eid, "42", at=good_at)
    await _record(
        hass, freezer, eid, "unavailable", at=now - timedelta(days=9, hours=12)
    )
    await _record(hass, freezer, eid, "unavailable", at=now)

    # With the DEFAULT 7d lookback the good value is outside the window -> floored
    # to the window edge (~7d lower bound).
    coord_default = await _recorder_coordinator(hass)
    freezer.move_to(now)
    await coord_default._async_update_data()
    rec_default = coord_default._downtime[device_id]
    assert rec_default.is_lower_bound is True
    assert (
        abs(
            (
                rec_default.since - (now - timedelta(days=RECORDER_LOOKBACK_DAYS))
            ).total_seconds()
        )
        < 3
    )

    # With a 30-day lookback the real good value at -10d is INSIDE the window and
    # is reconstructed as the TRUE since (not a lower bound, not floored).
    coord_big = await _recorder_coordinator(hass, **{CONF_RECORDER_LOOKBACK_DAYS: 30})
    freezer.move_to(now)
    await coord_big._async_update_data()
    rec_big = coord_big._downtime[device_id]
    assert rec_big.is_lower_bound is False, "true reading within new window"
    assert abs((rec_big.since - good_at).total_seconds()) < 5, (
        f"expected true since ~{good_at}, got {rec_big.since}"
    )


async def test_larger_lookback_floor_only_beyond_available_data(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With a 30d lookback but a device trimmed at 12d (no rows before -12d and no
    good value), the floor lands at the trim edge (~12d), not the 30d window."""
    now = dt_util.utcnow()
    device_id, (eid,) = await _make_device(hass, "longtrim")

    trim_days = 12
    await _record(hass, freezer, eid, "unavailable", at=now - timedelta(days=trim_days))
    await _record(hass, freezer, eid, "unavailable", at=now)

    coordinator = await _recorder_coordinator(hass, **{CONF_RECORDER_LOOKBACK_DAYS: 30})
    freezer.move_to(now)
    await coordinator._async_update_data()

    rec = coordinator._downtime[device_id]
    assert rec.is_lower_bound is True
    trim_edge = now - timedelta(days=trim_days)
    assert abs((rec.since - trim_edge).total_seconds()) < 3, (
        f"expected floor at trim edge {trim_edge}, got {rec.since}"
    )
    # NOT the 30d window edge.
    assert rec.since > now - timedelta(days=20)
