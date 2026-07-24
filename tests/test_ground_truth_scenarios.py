# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Ground-truth scenario suite — representative device shapes from real Home
Assistant installs, each asserting the REQUIRED verdict (the spec), not merely
"what the code does".

Every scenario models a realistic device situation and is written so it FAILS if
the protecting fix were reverted. The requirement each
test guards is named in its docstring/comment along with the fixing commit.

Scenarios:
  1. ESPHome "Illuminance Sensor" — genuinely dead ~4 days, MUST be flagged
     DEVICE_OFFLINE_CONFIRMED with its true since even though Vigil was set up
     during a recent restart.  (guards commit 4bb1703 — boot grace must exempt
     recorder-resolved outages, not just floors.)
  2. Dead Xiaomi plant "plant_a" — MUST be flagged >=7d NO_SIGNAL; a
     annotation_notes recent value must not rescue it or supply the since.
     (guards d1a7aef + 56ad392 — recorder queries the same entities as the
     availability check, and always floors a no-good offline device.)
  3. iBeacon/irk presence tracker — MUST NOT be flagged (presence-only device).
     (guards eea26e5 — device_tracker is not a data entity / recorder-blind.)
  4. Plant B — partially reporting, ALIVE — MUST NOT be flagged.
"""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EntityCategory,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.vigil.const import (
    CONF_AVAILABILITY_IGNORED_PLATFORMS,
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_STARTUP_IGNORE_SECONDS,
    DATA_BOOT_TIME,
    DOMAIN,
    RECORDER_LOOKBACK_DAYS,
)
from custom_components.vigil.coordinator import VigilCoordinator
from custom_components.vigil.detection.engines.engine2_unavailability import is_offline
from custom_components.vigil.detection.inputs import build_device_tuples
from custom_components.vigil.learning.interval_learner import IntervalLearner
from custom_components.vigil.models import IssueKind
from tests.helpers import NO_EXCLUSIONS, _add_connectivity, _entry, _settle


def _vigil_options(**overrides: object) -> dict[str, object]:
    opts: dict[str, object] = {
        CONF_GRACE_PERIOD_MINUTES: 15,
        CONF_STARTUP_IGNORE_SECONDS: 0,
        CONF_BATTERY_GRACE_MULTIPLIER: 2.0,
        # Models the real install: these annotation platforms are configured as
        # ignored for availability.
        CONF_AVAILABILITY_IGNORED_PLATFORMS: ["annotation_notes", "fleet_meta"],
    }
    opts.update(overrides)
    return opts


async def _make_coordinator(
    hass: HomeAssistant, **option_overrides: object
) -> VigilCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options=_vigil_options(**option_overrides)
    )
    entry.add_to_hass(hass)
    learner = IntervalLearner(hass)
    await learner.async_load()
    return VigilCoordinator(hass, entry, learner)


# ---------------------------------------------------------------------------
# Scenario 1 — ESPHome "Illuminance Sensor", genuinely dead ~4 days.
# REQUIREMENT (guards commit 4bb1703): a recorder-resolved REAL outage that
# predates the current boot must still fire — with its true since, not reset to
# boot — even when Vigil was set up during a recent restart. Connectivity DOWN
# (a connectivity binary_sensor reads "off") → DEVICE_OFFLINE_CONFIRMED, and
# since_is_lower_bound is False because the recorder has a real last-good value.
# Reverting 4bb1703 (so the boot grace also resets recorder-resolved records)
# would move the clock to boot and the device would sit inside grace → no flag.
# ---------------------------------------------------------------------------
async def test_esphome_illuminance_dead_4d_flagged_with_true_since(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    now = dt_util.utcnow()
    dead_at = now - timedelta(days=4)
    boot_time = now - timedelta(minutes=10)  # Vigil set up during a recent restart

    esp = _entry(hass, "esphome", "Illuminance Sensor")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=esp.entry_id,
        identifiers={("esphome", "lux_node")},
    )

    # ESPHome data sensors — all "unavailable" now; these are the entities the
    # recorder seed queries. They had good values until ~4 days ago.
    data_eids: list[str] = []
    data_specs = [
        ("illuminance", "illuminance"),
        ("actual_gain", None),
        ("full_spectrum_light", None),
        ("infrared_light", None),
        ("visible_light", "illuminance"),
        ("integration_time", None),
        ("ip", None),
        ("last_boot", "timestamp"),
        ("mac", None),
    ]
    for name, cls in data_specs:
        e = ent_reg.async_get_or_create(
            "sensor",
            "esphome",
            f"illum_{name}",
            device_id=device.id,
            original_device_class=cls,
        )
        data_eids.append(e.entity_id)

    # Connectivity binary_sensors — "off" → DOWN. status (esphome) + ping_2
    # (device_pulse). These are signal entities, excluded from availability, but
    # they DRIVE connectivity to DOWN.
    status = _add_connectivity(ent_reg, device, "illum_status", integration="esphome")
    ping2 = _add_connectivity(
        ent_reg, device, "illum_ping_2", integration="device_pulse"
    )

    # device_pulse last_response_time sensor — "unknown".
    last_resp = ent_reg.async_get_or_create(
        "sensor",
        "device_pulse",
        "illum_last_response_time",
        device_id=device.id,
    )

    # fleet_meta placeholder sensors — literal "None".
    fleet_eids: list[str] = []
    for name in (
        "pinned_esphome_version",
        "scheduled_one_time_upgrade",
        "upgrade_schedule",
    ):
        e = ent_reg.async_get_or_create(
            "sensor",
            "fleet_meta",
            f"illum_{name}",
            device_id=device.id,
        )
        fleet_eids.append(e.entity_id)

    # restart buttons + update entity (no live telemetry; ignored domains).
    restart = ent_reg.async_get_or_create(
        "button",
        "esphome",
        "illum_restart",
        device_id=device.id,
    )
    restart_safe = ent_reg.async_get_or_create(
        "button",
        "esphome",
        "illum_restart_safe_mode",
        device_id=device.id,
    )
    update = ent_reg.async_get_or_create(
        "update",
        "esphome",
        "illum_firmware",
        device_id=device.id,
    )

    # --- record real history: good until ~4d ago, then dead ---
    freezer.move_to(dead_at - timedelta(hours=1))
    for eid in data_eids:
        hass.states.async_set(eid, "100")
    await async_wait_recording_done(hass)
    freezer.move_to(dead_at)
    for eid in data_eids:
        hass.states.async_set(eid, "unavailable")
    await async_wait_recording_done(hass)

    # --- live state at "now" (post-restart) ---
    freezer.move_to(now)
    for eid in data_eids:
        hass.states.async_set(eid, "unavailable")
    hass.states.async_set(status.entity_id, "off")
    hass.states.async_set(ping2.entity_id, "off")
    hass.states.async_set(last_resp.entity_id, "unknown")
    for eid in fleet_eids:
        hass.states.async_set(eid, "None")
    hass.states.async_set(restart.entity_id, "unavailable")
    hass.states.async_set(restart_safe.entity_id, "unavailable")
    hass.states.async_set(update.entity_id, "off")
    await async_wait_recording_done(hass)

    # Vigil set up during a recent restart → records the boot anchor.
    hass.set_state(CoreState.starting)
    hass.data.pop(DATA_BOOT_TIME, None)
    hass.data[DATA_BOOT_TIME] = boot_time
    coordinator = await _make_coordinator(hass)
    assert coordinator._boot_time == boot_time
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    freezer.move_to(now)
    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device.id]
    assert offline, (
        "ESPHome node dead ~4 days (recorder-proven, connectivity DOWN) must be "
        "flagged even though Vigil set up during a recent restart"
    )
    issue = offline[0]
    assert issue.kind is IssueKind.DEVICE_OFFLINE_CONFIRMED
    assert issue.since_is_lower_bound is False
    duration = issue.duration_seconds(dt_util.utcnow())
    assert duration is not None
    # True since ~4 days — NOT reset to the 10-min-ago boot.
    assert 3.5 * 24 * 3600 < duration < 4.5 * 24 * 3600, (
        f"expected ~4d true since, got {duration}s — boot grace wrongly reset a "
        "recorder-resolved outage to boot (commit 4bb1703 reverted?)"
    )
    await _settle(hass, esp)


# ---------------------------------------------------------------------------
# Scenario 2 — dead Xiaomi plant "plant_a", dead the whole >=7d window.
# REQUIREMENT (guards d1a7aef + 56ad392): the 6 xiaomi sensors have rows but no
# good value in the window → floored (>= window) lower bound, connectivity
# UNKNOWN → DEVICE_OFFLINE_NO_SIGNAL. An annotation_notes entity that keeps a RECENT
# good value must NOT supply the since nor rescue the device. Reverting d1a7aef
# (so the recorder seed queries annotation_notes too) would let that recent value
# reset the outage clock → device sits behind grace, not flagged / not floored.
# ---------------------------------------------------------------------------
async def test_xiaomi_plant_a_dead_7d_floored_not_rescued_by_annotation(
    recorder_mock: object,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    now = dt_util.utcnow()

    xiaomi = _entry(hass, "xiaomi_ble", "Plant A")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=xiaomi.entry_id,
        identifiers={("xiaomi_ble", "aa:bb:cc:00:00:01")},
    )

    data_eids: list[str] = []
    for cls in ("moisture", "conductivity", "illuminance", "temperature"):
        e = ent_reg.async_get_or_create(
            "sensor",
            "xiaomi_ble",
            f"plant_a_{cls}",
            device_id=device.id,
            original_device_class=cls,
        )
        data_eids.append(e.entity_id)
    for cls in ("signal_strength", "battery"):
        e = ent_reg.async_get_or_create(
            "sensor",
            "xiaomi_ble",
            f"plant_a_{cls}",
            device_id=device.id,
            entity_category=EntityCategory.DIAGNOSTIC,
            original_device_class=cls,
        )
        data_eids.append(e.entity_id)

    # 5 annotation_notes annotation entities. RegistryEntry.platform == annotation_notes
    # (the registry create platform == 2nd positional arg).
    battery_type = ent_reg.async_get_or_create(
        "sensor",
        "annotation_notes",
        "plant_a_battery_type",
        device_id=device.id,
    )
    bn_other = []
    for name in (
        "battery_low",
        "battery_plus",
        "battery_last_replaced",
        "battery_quantity",
    ):
        e = ent_reg.async_get_or_create(
            "sensor",
            "annotation_notes",
            f"plant_a_{name}",
            device_id=device.id,
        )
        bn_other.append(e.entity_id)

    # --- record history ---
    # 8 days ago (before lookback edge): a genuine reading on the xiaomi sensors.
    freezer.move_to(now - timedelta(days=RECORDER_LOOKBACK_DAYS + 1))
    for eid in data_eids:
        hass.states.async_set(eid, "10")
    hass.states.async_set(battery_type.entity_id, "CR2032")
    await async_wait_recording_done(hass)
    # 7.5 days ago: xiaomi sensors die (no good value inside the window).
    freezer.move_to(now - timedelta(days=RECORDER_LOOKBACK_DAYS, hours=12))
    for eid in data_eids:
        hass.states.async_set(eid, "unavailable")
    await async_wait_recording_done(hass)
    # ~now: xiaomi dead; annotation_notes battery_type has a RECENT good value.
    freezer.move_to(now)
    for eid in data_eids:
        hass.states.async_set(eid, "unavailable")
    hass.states.async_set(battery_type.entity_id, "CR2032")
    for eid in bn_other:
        hass.states.async_set(eid, "unknown")
    await async_wait_recording_done(hass)

    coordinator = await _make_coordinator(hass)
    freezer.move_to(now)
    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device.id]
    assert offline, (
        "dead xiaomi plant must be flagged even though a annotation_notes entity "
        "still reports a recent good value (commit d1a7aef reverted?)"
    )
    issue = offline[0]
    assert issue.kind is IssueKind.DEVICE_OFFLINE_NO_SIGNAL
    assert issue.since_is_lower_bound is True
    duration = issue.duration_seconds(dt_util.utcnow())
    assert duration is not None
    assert duration > 5 * 24 * 3600, (
        f"expected >=~7d floor, got {duration}s — annotation_notes value leaked "
        "into the recorder seed and reset the outage clock"
    )
    await _settle(hass, xiaomi)


# ---------------------------------------------------------------------------
# Scenario 3 — iBeacon/irk presence tracker. ONLY a device_tracker (mqtt).
# REQUIREMENT (guards eea26e5): a presence-only device is never an offline
# candidate — device_tracker is an ignored (non-data) domain, so it has no data
# entities; it must never be flagged. Reverting eea26e5 (so a tracker counts as
# data) would make all_unavailable True → flagged.  No recorder needed (path is
# build_device_tuples + is_offline).
# ---------------------------------------------------------------------------
async def test_ibeacon_presence_tracker_never_flagged(hass: HomeAssistant) -> None:
    # Loadable "demo" config entry for clean teardown; the device_tracker entity
    # platform stays "mqtt" (faithful to the real iBeacon/irk MQTT tracker).
    mqtt = _entry(hass, "demo", "iBeacon Tracker")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt.entry_id, identifiers={("demo", "irk_beacon")}
    )
    tracker = ent_reg.async_get_or_create(
        "device_tracker",
        "mqtt",
        "irk_beacon",
        device_id=device.id,
    )
    # A tracker in a named zone/room (rather than home/not_home).
    hass.states.async_set(tracker.entity_id, "Guest Room")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert not t.data_entity_ids
    assert t.all_unavailable is False
    assert is_offline(t) is False
    await _settle(hass, mqtt)


# ---------------------------------------------------------------------------
# Scenario 4 — Plant B, partially reporting → ALIVE.
# REQUIREMENT: a device with at least one real value flowing (illuminance "236",
# temperature "72.5") is alive even though moisture/conductivity read "0".
# all_unavailable False → is_offline False → not flagged. (Path: tuples +
# is_offline.)
# ---------------------------------------------------------------------------
async def test_plant_b_partial_report_is_alive(hass: HomeAssistant) -> None:
    xiaomi = _entry(hass, "xiaomi_ble", "Plant B")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=xiaomi.entry_id,
        identifiers={("xiaomi_ble", "aa:bb:cc:00:00:02")},
    )
    values = {
        "illuminance": "236",
        "temperature": "72.5",
        "moisture": "0",
        "conductivity": "0",
    }
    for cls, val in values.items():
        e = ent_reg.async_get_or_create(
            "sensor",
            "xiaomi_ble",
            f"plant_b_{cls}",
            device_id=device.id,
            original_device_class=cls,
        )
        hass.states.async_set(e.entity_id, val)

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.data_entity_ids
    assert t.all_unavailable is False
    assert is_offline(t) is False
    await _settle(hass, xiaomi)
