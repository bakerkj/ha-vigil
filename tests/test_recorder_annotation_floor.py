# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Recorder scenario: an all-unavailable device with an annotation_notes annotation
entity (which stays *available* and keeps a recent good recorder value) must still
be floored to ``>= window`` and flagged immediately — not held behind grace.

This drives the REAL recorder (like test_recorder_e2e.py) using the loadable
``demo`` domain for clean teardown. It models the xiaomi_ble plant shape:

  * several data sensors, all dead the whole lookback window (no good value);
  * a annotation_notes annotation sensor that keeps reporting a real value
    (e.g. "CR2032") right up to the present — because annotation integrations do
    not reflect device reachability.

The availability judgement in connectivity.py already excludes the annotation
entity from the all-unavailable decision (the device IS offline). The bug is that
the recorder *seeding* path (_seed_recorder_downtime) does NOT exclude it, so the
annotation entity's recent good value makes the recorder reconstruction treat the
device as "last seen ~now", seeding a fresh offline_since instead of flooring.
With connectivity UNKNOWN -> grace = grace_period * battery_multiplier, the device
then sits inside grace and is never flagged.
"""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.vigil.const import (
    CONF_AVAILABILITY_IGNORED_PLATFORMS,
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_GRACE_PERIOD_MINUTES,
    RECORDER_LOOKBACK_DAYS,
)
from custom_components.vigil.coordinator import VigilCoordinator
from custom_components.vigil.models import IssueKind
from tests.helpers import _make_device, _recorder_coordinator


async def _run_cycle(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, with_annotation: bool
) -> tuple[VigilCoordinator, str]:
    """Build a dead xiaomi-shaped device (optionally with a annotation_notes
    annotation sensor that keeps a recent good value), record its history, and
    run one Vigil cycle. Returns (coordinator, device_id)."""
    now = dt_util.utcnow()

    # Two data sensors (data platform == "demo", domain "sensor").
    device_id, data_eids = await _make_device(hass, "plant", 2)

    # The annotation sensor. We register it under the "annotation_notes" platform so
    # connectivity.py's annotation filter recognizes it. It stays AVAILABLE the
    # whole time (annotation entities don't track reachability).
    annotation_eid = None
    if with_annotation:
        ent_reg = er.async_get(hass)
        ann = ent_reg.async_get_or_create(
            "sensor", "annotation_notes", "plant_battery_type", device_id=device_id
        )
        annotation_eid = ann.entity_id

    # --- record real history (monotonic forward in time) ---
    # 8 days ago: a genuine reading on the data sensors (before the lookback edge).
    freezer.move_to(now - timedelta(days=RECORDER_LOOKBACK_DAYS + 1))
    for eid in data_eids:
        hass.states.async_set(eid, "50")
    if annotation_eid:
        hass.states.async_set(annotation_eid, "CR2032")
    await async_wait_recording_done(hass)

    # 7.5 days ago: the data sensors die (before the lookback window edge). The
    # annotation sensor keeps reporting its value.
    freezer.move_to(now - timedelta(days=RECORDER_LOOKBACK_DAYS, hours=12))
    for eid in data_eids:
        hass.states.async_set(eid, "unavailable")
    await async_wait_recording_done(hass)

    # ~now: data sensors still unavailable; annotation sensor still reporting a
    # fresh good value (this is what leaks into the recorder seed).
    freezer.move_to(now)
    for eid in data_eids:
        hass.states.async_set(eid, "unavailable")
    if annotation_eid:
        hass.states.async_set(annotation_eid, "CR2032")
    await async_wait_recording_done(hass)

    coordinator = await _recorder_coordinator(
        hass,
        **{
            CONF_GRACE_PERIOD_MINUTES: 15,
            # Default 2.0; UNKNOWN connectivity uses this -> 30-min effective grace.
            CONF_BATTERY_GRACE_MULTIPLIER: 2.0,
            # Real install: annotation_notes configured as an ignored annotation platform.
            CONF_AVAILABILITY_IGNORED_PLATFORMS: ["annotation_notes"],
        },
    )
    return coordinator, device_id


async def test_annotation_value_does_not_block_floor(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The bug: a dead device whose ONLY recent good recorder value belongs to a
    annotation_notes annotation entity must still floor to ``>= window`` and be
    flagged on the FIRST cycle (not deferred behind the UNKNOWN/battery grace)."""
    coordinator, device_id = await _run_cycle(hass, freezer, with_annotation=True)

    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    assert offline, (
        "dead device must be flagged on the first cycle even though a "
        "annotation_notes annotation entity still has a recent good recorder value"
    )
    issue = offline[0]
    assert issue.kind is IssueKind.DEVICE_OFFLINE_NO_SIGNAL
    # Floored to the window edge -> lower bound, very old (NOT ~now behind grace).
    assert issue.since_is_lower_bound is True
    # Pin the floor to the exact recorder window edge (now - lookback), so a
    # floor-to-wrong-edge bug is caught — the loose ">5d" check would miss it.
    assert issue.since is not None
    window_floor = dt_util.utcnow() - timedelta(days=RECORDER_LOOKBACK_DAYS)
    assert abs((issue.since - window_floor).total_seconds()) < 5, (
        f"expected since == window floor {window_floor}, got {issue.since}"
    )
    duration = issue.duration_seconds(dt_util.utcnow())
    assert duration is not None
    assert duration > 5 * 24 * 3600, (
        f"expected >=~7d floor, got {duration}s — annotation value leaked into "
        "the recorder seed and reset the outage clock"
    )


async def test_no_annotation_control_is_flagged(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Control: the identical device WITHOUT a annotation_notes entity is correctly
    floored and flagged on the first cycle (proves the harness, isolates cause)."""
    coordinator, device_id = await _run_cycle(hass, freezer, with_annotation=False)

    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device_id]
    assert offline, "dead device with no annotation must be flagged on first cycle"
    assert offline[0].since_is_lower_bound is True
