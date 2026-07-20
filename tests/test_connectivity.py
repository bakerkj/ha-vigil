# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil.detection.engines.engine2_unavailability import is_offline
from custom_components.vigil.detection.inputs import (
    _primary_config_entry,
    build_device_tuples,
)
from custom_components.vigil.models import ConnectivityState
from custom_components.vigil.models import ExclusionConfig
from custom_components.vigil.selectors import EntitySelector
from tests.helpers import (
    NO_EXCLUSIONS,
    _add_connectivity,
    _demo_device,
    _entry,
    _exclude,
)


def _ignore_platforms(*platforms: str) -> ExclusionConfig:
    """An ExclusionConfig that only marks the given platforms as ignored."""
    return _exclude(ignored_platforms=platforms)


async def test_all_unavailable_device(hass: HomeAssistant) -> None:
    """A device whose only entities are unavailable yields all_unavailable."""
    entry, ent_reg, device = _demo_device(hass, "d1")
    ent = ent_reg.async_get_or_create("sensor", "demo", "u1", device_id=device.id)
    hass.states.async_set(ent.entity_id, "unavailable")

    tuples = build_device_tuples(hass, NO_EXCLUSIONS)
    by_id = {t.device_id: t for t in tuples}

    assert device.id in by_id
    t = by_id[device.id]
    assert t.all_unavailable is True
    assert t.config_entry_state is ConfigEntryState.LOADED
    assert t.connectivity_state is ConnectivityState.UNKNOWN
    assert t.connectivity_source == "none"


async def test_connectivity_binary_sensor_up(hass: HomeAssistant) -> None:
    """A same-device connectivity binary_sensor 'on' resolves to UP (P1)."""
    entry, ent_reg, device = _demo_device(hass, "d2")
    sensor = ent_reg.async_get_or_create("sensor", "demo", "v1", device_id=device.id)
    conn = _add_connectivity(ent_reg, device, "conn1")
    hass.states.async_set(sensor.entity_id, "23")
    hass.states.async_set(conn.entity_id, "on")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert t.connectivity_source == "connectivity_binary_sensor"
    assert t.all_unavailable is False


async def test_user_device_class_override_wins(hass: HomeAssistant) -> None:
    """A user's device_class override takes precedence over the integration's
    original (HA convention): overriding to 'connectivity' makes it a signal."""
    entry, ent_reg, device = _demo_device(hass, "dcov")
    bs = ent_reg.async_get_or_create(
        "binary_sensor",
        "demo",
        "dcov_bs",
        device_id=device.id,
        original_device_class="problem",  # integration default is NOT connectivity
    )
    ent_reg.async_update_entity(
        bs.entity_id, device_class="connectivity"
    )  # user override
    hass.states.async_set(bs.entity_id, "on")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert t.connectivity_source == "connectivity_binary_sensor"


async def test_ignore_connectivity_skips_mislabeled_sensor(hass: HomeAssistant) -> None:
    """A vigil.yaml ignore rule drops a mislabeled device_class=connectivity sensor
    (the Litter-Robot hopper accessory) from connectivity resolution, so the device
    resolves UP from its real ``online`` sensor instead of DOWN from the hopper."""
    # Left NOT_LOADED so teardown doesn't try to import the real litterrobot
    # component; connectivity resolves from the entities regardless of entry state.
    entry = MockConfigEntry(domain="litterrobot", title="Litter-Robot")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("litterrobot", "lr5")}
    )
    hopper = ent_reg.async_get_or_create(
        "binary_sensor",
        "litterrobot",
        "hopper",
        suggested_object_id="robot_hopper_connected",
        device_id=device.id,
        original_device_class="connectivity",
    )
    online = ent_reg.async_get_or_create(
        "binary_sensor",
        "litterrobot",
        "online",
        suggested_object_id="robot_online",
        device_id=device.id,
        original_device_class="connectivity",
    )
    hass.states.async_set(hopper.entity_id, "off")  # accessory not attached
    hass.states.async_set(online.entity_id, "on")  # device reachable

    ignore = [
        EntitySelector(
            integration="litterrobot",
            device_class="connectivity",
            entity_id_suffix="_hopper_connected",
        )
    ]
    tuples = build_device_tuples(hass, NO_EXCLUSIONS, ignore_connectivity=ignore)
    t = {x.device_id: x for x in tuples}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert t.connectivity_source == "connectivity_binary_sensor"


async def test_ignore_connectivity_drops_lone_signal_to_unknown(
    hass: HomeAssistant,
) -> None:
    """When the ignored sensor is the ONLY connectivity signal, the device loses
    that signal entirely (resolves UNKNOWN), proving the rule removes it."""
    entry, ent_reg, device = _demo_device(hass, "lone")
    data = ent_reg.async_get_or_create("sensor", "demo", "v", device_id=device.id)
    conn = _add_connectivity(ent_reg, device, "conn")
    hass.states.async_set(data.entity_id, "5")
    hass.states.async_set(conn.entity_id, "off")

    ignore = [EntitySelector(integration="demo", device_class="connectivity")]
    tuples = build_device_tuples(hass, NO_EXCLUSIONS, ignore_connectivity=ignore)
    t = {x.device_id: x for x in tuples}[device.id]
    assert t.connectivity_state is ConnectivityState.UNKNOWN
    assert t.connectivity_source == "none"


async def test_user_override_removes_connectivity_class(hass: HomeAssistant) -> None:
    """Overriding an original 'connectivity' class to something else stops the
    entity being treated as a connectivity signal (the user's intent wins)."""
    entry, ent_reg, device = _demo_device(hass, "dcov2")
    bs = _add_connectivity(ent_reg, device, "dcov2_bs")
    ent_reg.async_update_entity(bs.entity_id, device_class="problem")  # override away
    hass.states.async_set(bs.entity_id, "on")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UNKNOWN
    assert t.connectivity_source == "none"


async def test_excluded_device_skipped(hass: HomeAssistant) -> None:
    """A device on the exclusion list produces no tuple."""
    entry, ent_reg, device = _demo_device(hass, "d3")
    ent = ent_reg.async_get_or_create("sensor", "demo", "w1", device_id=device.id)
    hass.states.async_set(ent.entity_id, "5")

    exclusions = _exclude(device_ids={device.id})
    assert all(t.device_id != device.id for t in build_device_tuples(hass, exclusions))


async def test_unknown_entity_does_not_mask_unavailable(hass: HomeAssistant) -> None:
    """A stuck-`unknown` entity must not block all_unavailable for the device."""
    entry, ent_reg, device = _demo_device(hass, "mix1")
    gone = ent_reg.async_get_or_create("sensor", "demo", "mix1_a", device_id=device.id)
    stuck = ent_reg.async_get_or_create("sensor", "demo", "mix1_b", device_id=device.id)
    hass.states.async_set(gone.entity_id, "unavailable")
    hass.states.async_set(stuck.entity_id, "unknown")  # no evidence, not a value

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    # The unknown entity is ignored; the only definite entity is unavailable.
    assert t.all_unavailable is True


async def test_all_unknown_device_is_not_reporting(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A device whose entities are only unknown counts as not reporting (unknown is
    treated like unavailable — no real value flowing)."""
    # Pin the instant the entity goes unknown so offline_since can be asserted to
    # an exact value (not merely "not None").
    unknown_at = dt_util.utcnow()
    freezer.move_to(unknown_at)

    entry, ent_reg, device = _demo_device(hass, "unk1")
    e = ent_reg.async_get_or_create("sensor", "demo", "unk1_a", device_id=device.id)
    hass.states.async_set(e.entity_id, "unknown")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.all_unavailable is True
    assert t.any_unavailable is True
    # offline_since is the (frozen) instant the entity went unknown, not "now-ish".
    assert t.offline_since == unknown_at


async def test_up_connectivity_vetoes_all_unknown_offline(
    hass: HomeAssistant,
) -> None:
    """A reachable device (connectivity 'on') with a data sensor stuck 'unknown'
    (never produced a first value, nothing actually unavailable) must not be
    flagged offline — the positive UP signal vetoes it."""
    entry, ent_reg, device = _demo_device(hass, "upunk")
    data = ent_reg.async_get_or_create("sensor", "demo", "upunk_a", device_id=device.id)
    conn = _add_connectivity(ent_reg, device, "upunk_conn")
    hass.states.async_set(data.entity_id, "unknown")  # never produced a value
    hass.states.async_set(conn.entity_id, "on")  # but the device is reachable

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert t.all_unavailable is False


async def test_up_connectivity_still_offline_when_data_unavailable(
    hass: HomeAssistant,
) -> None:
    """The UP veto only spares an all-unknown set: a data entity that actually goes
    unavailable while connectivity reads 'on' still counts as down."""
    entry, ent_reg, device = _demo_device(hass, "upunavail")
    data = ent_reg.async_get_or_create(
        "sensor", "demo", "upunavail_a", device_id=device.id
    )
    conn = _add_connectivity(ent_reg, device, "upunavail_conn")
    hass.states.async_set(data.entity_id, "unavailable")
    hass.states.async_set(conn.entity_id, "on")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert t.all_unavailable is True


async def test_update_entity_excluded_from_availability(hass: HomeAssistant) -> None:
    """An always-available `update` entity must not mask an offline device."""
    entry, ent_reg, device = _demo_device(hass, "esp1")
    data = ent_reg.async_get_or_create(
        "sensor", "demo", "esp1_lux", device_id=device.id
    )
    upd = ent_reg.async_get_or_create("update", "demo", "esp1_fw", device_id=device.id)
    hass.states.async_set(data.entity_id, "unavailable")
    # ESPHome keeps the firmware-update entity reporting "off" while offline.
    hass.states.async_set(upd.entity_id, "off")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.all_unavailable is True
    assert t.offline_since is not None


async def test_annotation_platform_and_button_excluded(hass: HomeAssistant) -> None:
    """Configured annotation-platform entities + button must not mask an offline
    device (real plant-sensor / CPAP case)."""
    entry, ent_reg, device = _demo_device(hass, "plant1")
    moisture = ent_reg.async_get_or_create(
        "sensor", "demo", "plant1_moisture", device_id=device.id
    )
    # Annotation entity from the annotation_notes integration (platform).
    battery_type = ent_reg.async_get_or_create(
        "sensor", "annotation_notes", "plant1_battery_type", device_id=device.id
    )
    btn = ent_reg.async_get_or_create(
        "button", "annotation_notes", "plant1_replaced", device_id=device.id
    )
    hass.states.async_set(moisture.entity_id, "unavailable")
    hass.states.async_set(battery_type.entity_id, "CR2032")  # annotation, lingers
    hass.states.async_set(btn.entity_id, "2025-01-01T00:00:00+00:00")

    excl = _ignore_platforms("annotation_notes")
    t = {x.device_id: x for x in build_device_tuples(hass, excl)}[device.id]
    assert t.all_unavailable is True
    assert t.offline_since is not None


async def test_unconfigured_annotation_platform_is_not_ignored(
    hass: HomeAssistant,
) -> None:
    """Annotation platforms are not built-in: with none configured, a lingering
    annotation_notes sensor still counts and keeps the device from all-unavailable."""
    entry, ent_reg, device = _demo_device(hass, "plant3")
    moisture = ent_reg.async_get_or_create(
        "sensor", "demo", "plant3_moisture", device_id=device.id
    )
    battery_type = ent_reg.async_get_or_create(
        "sensor", "annotation_notes", "plant3_battery_type", device_id=device.id
    )
    hass.states.async_set(moisture.entity_id, "unavailable")
    hass.states.async_set(battery_type.entity_id, "CR2032")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.all_unavailable is False  # nothing configured -> not ignored


async def test_fleet_meta_placeholder_does_not_mask_offline(
    hass: HomeAssistant,
) -> None:
    """An ignored-platform metadata sensor holding a placeholder value ("None")
    rather than going unavailable must not mask a dead device (excluded by
    platform, not by inspecting the value)."""
    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("esphome", "node1")}
    )
    lux = ent_reg.async_get_or_create(
        "sensor", "esphome", "node1_illuminance", device_id=device.id
    )
    pinned = ent_reg.async_get_or_create(
        "sensor", "fleet_meta", "node1_pinned_esphome_version", device_id=device.id
    )
    schedule = ent_reg.async_get_or_create(
        "sensor", "fleet_meta", "node1_upgrade_schedule", device_id=device.id
    )
    hass.states.async_set(lux.entity_id, "unavailable")
    hass.states.async_set(pinned.entity_id, "None")  # placeholder, lingers
    hass.states.async_set(schedule.entity_id, "None")

    excl = _ignore_platforms("fleet_meta")
    t = {x.device_id: x for x in build_device_tuples(hass, excl)}[device.id]
    assert t.all_unavailable is True
    assert t.offline_since is not None


async def test_diagnostic_sensor_still_counts_for_availability(
    hass: HomeAssistant,
) -> None:
    """A genuine diagnostic sensor still reporting is NOT excluded — it keeps the
    device from reading fully unavailable (don't blanket-exclude diagnostics)."""
    entry, ent_reg, device = _demo_device(hass, "plant2")
    moisture = ent_reg.async_get_or_create(
        "sensor", "demo", "plant2_moisture", device_id=device.id
    )
    rssi = ent_reg.async_get_or_create(
        "sensor",
        "demo",
        "plant2_rssi",
        device_id=device.id,
        entity_category=EntityCategory.DIAGNOSTIC,
        original_device_class="signal_strength",
    )
    hass.states.async_set(moisture.entity_id, "unavailable")
    hass.states.async_set(rssi.entity_id, "-60")  # diagnostic, still reporting

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.all_unavailable is False
    assert t.any_unavailable is True


async def test_battery_device_detected(hass: HomeAssistant) -> None:
    """A battery device_class entity (registry class) marks the device battery."""
    entry, ent_reg, device = _demo_device(hass, "d4")
    batt = ent_reg.async_get_or_create(
        "sensor",
        "demo",
        "batt1",
        device_id=device.id,
        original_device_class="battery",
    )
    hass.states.async_set(batt.entity_id, "80")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.is_battery is True


async def test_battery_detected_via_state_attribute(hass: HomeAssistant) -> None:
    """A battery device_class in state attributes (no registry class) counts."""
    entry, ent_reg, device = _demo_device(hass, "d5")
    ent = ent_reg.async_get_or_create("sensor", "demo", "ba1", device_id=device.id)
    hass.states.async_set(ent.entity_id, "55", {"device_class": "battery"})

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.is_battery is True


async def test_zwave_node_status_dead_is_down(hass: HomeAssistant) -> None:
    """A zwave_js node_status of 'dead' resolves DOWN (P2), not a data entity."""
    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("zwave_js", "node9")}
    )
    data = ent_reg.async_get_or_create(
        "sensor", "zwave_js", "node9.air", device_id=device.id
    )
    status = ent_reg.async_get_or_create(
        "sensor", "zwave_js", "node9.node_status", device_id=device.id
    )
    hass.states.async_set(data.entity_id, "unavailable")
    hass.states.async_set(status.entity_id, "dead")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.DOWN
    assert t.connectivity_source == "zwave_node_status"
    # node_status is a signal entity, so availability is judged over the data
    # sensor alone — which is unavailable.
    assert t.all_unavailable is True


async def test_zwave_node_status_asleep_is_unknown(hass: HomeAssistant) -> None:
    """A sleeping zwave node is UNKNOWN, never a hard DOWN."""
    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("zwave_js", "node10")}
    )
    data = ent_reg.async_get_or_create(
        "sensor", "zwave_js", "node10.temp", device_id=device.id
    )
    status = ent_reg.async_get_or_create(
        "sensor", "zwave_js", "node10.node_status", device_id=device.id
    )
    hass.states.async_set(data.entity_id, "21")
    hass.states.async_set(status.entity_id, "asleep")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UNKNOWN
    assert t.connectivity_source == "zwave_node_status_asleep"


async def test_priority_order_binary_sensor_beats_zwave(hass: HomeAssistant) -> None:
    """P1 (connectivity binary_sensor) short-circuits P2 (zwave node_status)."""
    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("zwave_js", "node11")}
    )
    conn = _add_connectivity(ent_reg, device, "p1")
    status = ent_reg.async_get_or_create(
        "sensor", "zwave_js", "node11.node_status", device_id=device.id
    )
    # P1 says UP, P2 would say DOWN — P1 must win.
    hass.states.async_set(conn.entity_id, "on")
    hass.states.async_set(status.entity_id, "dead")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert t.connectivity_source == "connectivity_binary_sensor"


async def test_mac_correlation_to_router_tracker(hass: HomeAssistant) -> None:
    """A same-MAC router device_tracker reporting 'home' resolves UP (P4). The
    entity platform (not config-entry domain) identifies the router source."""
    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    mac = "aa:bb:cc:dd:ee:01"

    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("demo", "m1")},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
    )
    data = ent_reg.async_get_or_create("sensor", "demo", "m1s", device_id=device.id)
    tracker = ent_reg.async_get_or_create(
        "device_tracker", "unifi", "client1", device_id=device.id
    )
    hass.states.async_set(data.entity_id, "9")
    hass.states.async_set(tracker.entity_id, "home")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert t.connectivity_source == "mac:unifi"


async def test_mac_tracker_up_does_not_veto_all_unknown(hass: HomeAssistant) -> None:
    """A weak shared-MAC router-tracker UP must NOT veto an all-`unknown` data set
    (the router seeing the MAC doesn't prove the device's integration is alive)."""
    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    mac = "aa:bb:cc:dd:ee:02"
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("demo", "mv")},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
    )
    data = ent_reg.async_get_or_create("sensor", "demo", "mvs", device_id=device.id)
    tracker = ent_reg.async_get_or_create(
        "device_tracker", "unifi", "mvclient", device_id=device.id
    )
    hass.states.async_set(data.entity_id, "unknown")  # never produced a value
    hass.states.async_set(tracker.entity_id, "home")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert t.connectivity_source == "mac:unifi"
    assert t.all_unavailable is True  # weak UP does not veto


async def test_merged_router_tracker_does_not_mask_dead_telemetry(
    hass: HomeAssistant,
) -> None:
    """A router device_tracker merged onto a device via shared MAC is a
    reachability signal, not telemetry: reading "home" keeps connectivity UP but
    must not stop all-unavailable real sensors from being judged offline."""
    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("xiaomi_ble", "A4:C1:38:00:00:01")},
        connections={(dr.CONNECTION_NETWORK_MAC, "a4:c1:38:00:00:01")},
    )
    for cls in ("moisture", "temperature"):
        e = ent_reg.async_get_or_create(
            "sensor",
            "xiaomi_ble",
            f"plant_{cls}",
            device_id=device.id,
            original_device_class=cls,
        )
        hass.states.async_set(e.entity_id, "unavailable")
    tracker = ent_reg.async_get_or_create(
        "device_tracker", "aruba_instant_ap", "client_a", device_id=device.id
    )
    hass.states.async_set(tracker.entity_id, "home")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.UP
    assert tracker.entity_id in t.signal_entity_ids
    assert tracker.entity_id not in t.data_entity_ids
    assert t.all_unavailable is True
    assert is_offline(t) is True


async def test_primary_integration_prefers_real_owner_over_annotation(
    hass: HomeAssistant,
) -> None:
    """A device several integrations extend is attributed to its heaviest
    non-annotation owner, even when HA marked the annotation entry as primary."""
    fleet = MockConfigEntry(domain="node_fleet", title="Fleet app")
    fleet.add_to_hass(hass)
    fleet.mock_state(hass, ConfigEntryState.LOADED)
    real = MockConfigEntry(domain="node", title="Node")
    real.add_to_hass(hass)
    real.mock_state(hass, ConfigEntryState.LOADED)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    # Device created by the fleet (annotation) entry first -> HA marks it primary.
    device = dev_reg.async_get_or_create(
        config_entry_id=fleet.entry_id, identifiers={("node", "mini")}
    )
    # The real integration also owns the device (and most of its entities).
    dev_reg.async_get_or_create(
        config_entry_id=real.entry_id, identifiers={("node", "mini")}
    )
    fent = ent_reg.async_get_or_create(
        "sensor",
        "node_fleet",
        "pinned_version",
        device_id=device.id,
        config_entry=fleet,
    )
    hass.states.async_set(fent.entity_id, "None")
    for i in range(3):
        e = ent_reg.async_get_or_create(
            "sensor",
            "node",
            f"r{i}",
            device_id=device.id,
            config_entry=real,
        )
        hass.states.async_set(e.entity_id, "unavailable")

    tuples = build_device_tuples(hass, _ignore_platforms("node_fleet"))
    t = {x.device_id: x for x in tuples}[device.id]

    assert t.config_entry_domain == "node"  # the real home, not node_fleet
    assert t.config_entry_title == "Node"


@pytest.mark.parametrize(
    ("entries", "primary", "reg_domains", "ignored", "expected"),
    [
        # HA primary names a real integration: trusted even though the (non-
        # annotation) overlay "pulse" owns more of the device's entities.
        (["node", "pulse"], "node", ["node", "pulse", "pulse"], [], "node"),
        # HA primary names an ANNOTATION platform: overridden by the heaviest
        # real (non-annotation) owner.
        (
            ["node_fleet", "node"],
            "node_fleet",
            ["node_fleet", "node", "node", "node"],
            ["node_fleet"],
            "node",
        ),
        # Only the annotation's entities are enabled, but a real entry is linked:
        # still attributed to the real entry.
        (
            ["node", "node_fleet"],
            "node_fleet",
            ["node_fleet"] * 4,
            ["node_fleet"],
            "node",
        ),
        # The annotation is the ONLY home: stays attributed to it.
        (
            ["node_fleet"],
            "node_fleet",
            ["node_fleet"] * 4,
            ["node_fleet"],
            "node_fleet",
        ),
    ],
)
async def test_primary_config_entry_resolution(
    hass: HomeAssistant,
    entries: list[str],
    primary: str,
    reg_domains: list[str],
    ignored: list[str],
    expected: str,
) -> None:
    """_primary_config_entry prefers the heaviest real (non-annotation) owner,
    trusting HA's primary only when it names a real integration."""
    made: dict[str, MockConfigEntry] = {}
    for domain in entries:
        entry = MockConfigEntry(domain=domain)
        entry.add_to_hass(hass)
        made[domain] = entry
    device = SimpleNamespace(
        primary_config_entry=made[primary].entry_id,
        config_entries={made[d].entry_id for d in entries},
    )
    reg = [
        SimpleNamespace(config_entry_id=made[d].entry_id, platform=d)
        for d in reg_domains
    ]
    result = _primary_config_entry(hass, device, reg, frozenset(ignored))  # type: ignore[arg-type]
    assert result is not None and result.domain == expected


async def test_mac_router_tracker_away_resolves_down(hass: HomeAssistant) -> None:
    """A same-MAC router device_tracker reporting 'not_home' resolves DOWN (P4)."""
    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    mac = "aa:bb:cc:dd:ee:09"
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("demo", "m9")},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
    )
    data = ent_reg.async_get_or_create("sensor", "demo", "m9s", device_id=device.id)
    tracker = ent_reg.async_get_or_create(
        "device_tracker", "unifi", "client9", device_id=device.id
    )
    hass.states.async_set(data.entity_id, "unavailable")
    hass.states.async_set(tracker.entity_id, "not_home")

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.connectivity_state is ConnectivityState.DOWN
    assert t.connectivity_source == "mac:unifi"


async def test_one_bad_device_does_not_blank_the_cycle(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single device that raises during tuple assembly is logged and skipped —
    the rest of the cycle still yields tuples."""
    import custom_components.vigil.detection.inputs as conn_mod

    entry = _entry(hass, "demo", "Demo")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    good = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", "good")}
    )
    e = ent_reg.async_get_or_create("sensor", "demo", "goods", device_id=good.id)
    hass.states.async_set(e.entity_id, "1")
    bad = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", "bad")}
    )

    real = conn_mod._build_one_tuple

    def _maybe_boom(hass_, device, *a, **k):  # type: ignore[no-untyped-def]
        if device.id == bad.id:
            raise ValueError("boom")
        return real(hass_, device, *a, **k)

    monkeypatch.setattr(conn_mod, "_build_one_tuple", _maybe_boom)

    ids = {t.device_id for t in build_device_tuples(hass, NO_EXCLUSIONS)}
    assert good.id in ids  # the healthy device survives the bad one
    assert bad.id not in ids
