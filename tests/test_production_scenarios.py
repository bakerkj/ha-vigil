# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Real-world-shaped end-to-end scenarios (modeled on a large HA 2026.6 install).

These lock in behavior for device/state shapes the unit-level engine tests don't
reach through the real registry/state path. All data below is synthetic:

* the "unknown" vs "unavailable" distinction (BLE plant sensors),
* a sleepy multi-entity battery device (Engine 3 freshest-vs-fastest),
* the HA-restart grace-clock reset that makes a whole down cohort cross grace
  at once ~15 min after restart,
* integration_health friendly names overriding misleading entry titles (e.g. a
  server URL or an account email as an entry title).
"""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil.const import (
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_STALENESS_MULTIPLIER,
    CONF_STARTUP_IGNORE_SECONDS,
    DATA_BOOT_TIME,
)
from custom_components.vigil.models import IssueKind
from tests.helpers import _add_connectivity, _make_coordinator, _settle, seed_learner

# --- #5: "unknown" vs "unavailable" -----------------------------------------


async def test_all_unknown_device_flagged_offline_after_grace(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An all-``unknown`` device, past grace, IS reported offline (no signal).

    With no recorder in the test, downtime falls back to last_changed; once that
    exceeds the grace, the silent BLE device is flagged DEVICE_OFFLINE_NO_SIGNAL.
    """
    entry = MockConfigEntry(domain="xiaomi_ble", title="Plant Sensor")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("xiaomi_ble", "plant2")}
    )
    moisture = ent_reg.async_get_or_create(
        "sensor", "xiaomi_ble", "plant2_moisture", device_id=device.id
    )
    hass.states.async_set(moisture.entity_id, "unknown")

    coordinator = await _make_coordinator(
        hass,
        options={
            CONF_GRACE_PERIOD_MINUTES: 1,
            CONF_STARTUP_IGNORE_SECONDS: 0,
            CONF_BATTERY_GRACE_MULTIPLIER: 1.0,
        },
    )
    await coordinator._async_update_data()
    freezer.tick(timedelta(hours=2))
    data = await coordinator._async_update_data()

    offline = [i for i in data["devices_offline"] if i.device_id == device.id]
    assert offline, "all-unknown device should be flagged offline past grace"
    assert offline[0].kind is IssueKind.DEVICE_OFFLINE_NO_SIGNAL

    await _settle(hass, entry)


# --- #6: sleepy multi-entity battery device (Engine 3) -----------------------


async def test_sleepy_multi_entity_device_judged_on_freshest_report(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A sleepy device is judged on its freshest entity, not its oldest.

    Reproduces a Shelly Motion / xiaomi_ble shape: several entities on one
    device, all updated together each wake cycle. After a wake, the freshest
    report is recent even if an unchanging entity's ``last_changed`` is old.
    Engine 3 must learn each entity's cadence and judge the device on the
    freshest ``last_reported`` vs the fastest learned cadence — so a freshly
    woken device is never called silent, but one that overslept is.
    """
    now = dt_util.utcnow()

    entry = MockConfigEntry(domain="xiaomi_ble", title="Motion")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("xiaomi_ble", "motion")}
    )
    illum = ent_reg.async_get_or_create(
        "sensor", "xiaomi_ble", "motion_illuminance", device_id=device.id
    )
    battery = ent_reg.async_get_or_create(
        "sensor",
        "xiaomi_ble",
        "motion_battery",
        device_id=device.id,
        original_device_class="battery",
    )
    # device_pulse adds a connectivity ping; currently reachable (UP) so Engine 3
    # is eligible to judge staleness. It's a signal entity, excluded from the
    # staleness eligibility set (and never counted itself).
    ping = _add_connectivity(ent_reg, device, "motion_ping", integration="device_pulse")
    hass.states.async_set(ping.entity_id, "on")

    coordinator = await _make_coordinator(
        hass,
        options={
            CONF_STALENESS_MULTIPLIER: 3.0,
            CONF_STARTUP_IGNORE_SECONDS: 0,
        },
    )

    # Pre-learn a ~300s cadence for both data entities — a per-day max gap of 300s
    # over several recent days, spanning the warmup. (The time-horizon learner
    # needs days of history, so seed it directly rather than cycle-by-cycle.)
    for eid in (illum.entity_id, battery.entity_id):
        seed_learner(
            coordinator.learner,
            eid,
            gap_seconds=300.0,
            days=5,
            epoch=now - timedelta(days=5),
        )

    # Just woke (freshest report is "now") — must not be flagged silent even
    # though the battery's value is unchanged (only last_reported advances).
    freezer.move_to(now)
    hass.states.async_set(illum.entity_id, "70")
    hass.states.async_set(battery.entity_id, "80")
    data = await coordinator._async_update_data()
    assert all(i.device_id != device.id for i in data["stale_devices"])

    # Overslept well past 3 x learned cadence (~900s) with nothing reporting.
    freezer.move_to(now + timedelta(seconds=2000))
    data = await coordinator._async_update_data()
    stale = {i.device_id: i for i in data["stale_devices"]}
    assert device.id in stale
    assert stale[device.id].kind is IssueKind.SILENT_DEVICE

    # Avoid a teardown unload of the (real-component) xiaomi_ble mock entry.
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)

    await _settle(hass, entry)


# --- #7: HA-restart grace-clock reset cohort ---------------------------------


async def test_restart_resets_grace_clock_for_down_cohort(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A full HA restart defers grace for an already-down cohort via boot grace.

    On a real restart every entity is re-created, so its ``last_changed`` becomes
    "now"; the recorder is often not loaded yet at the first post-grace cycle, so
    Engine 2 falls back to the live ``offline_since``. A device that was already
    down therefore must not be flagged the instant the global startup grace lifts.
    The per-device boot grace (engine2_unavailability.py ~88-99) measures grace
    from the boot anchor for any non-recorder-resolved record whose ``since``
    predates the restart, so the whole already-down cohort only crosses grace
    ~15 min after the restart — together, not immediately.

    To exercise that branch (not a hand-rolled last_changed re-stamp) the
    coordinator is constructed during ``CoreState.starting`` with the boot anchor
    pinned to the frozen restart instant, exactly as in
    tests/test_boot_grace_repro.py. With ``STARTUP_IGNORE_SECONDS=0`` the global
    startup grace is inactive, so this isolates the per-device boot grace.
    """
    grace = timedelta(minutes=15)
    restart_instant = dt_util.utcnow()

    # Device went down an hour before the restart.
    freezer.move_to(restart_instant - timedelta(hours=1))
    demo = MockConfigEntry(domain="demo", title="Hub")
    demo.add_to_hass(hass)
    demo.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=demo.entry_id, identifiers={("demo", "cohort")}
    )
    data_sensor = ent_reg.async_get_or_create(
        "sensor", "demo", "cohort_s", device_id=device.id
    )
    conn = _add_connectivity(ent_reg, device, "cohort_c")
    # The pre-restart offline state. ``offline_since`` (= last_changed) is stale,
    # an hour old, and survives the reboot — this is the stale timestamp the boot
    # grace must move off of.
    hass.states.async_set(data_sensor.entity_id, "unavailable")
    hass.states.async_set(conn.entity_id, "off")

    # No recorder loaded → recorder seed is skipped (exactly like the first
    # post-grace cycle in prod), so the record is not recorder-resolved and the
    # boot-grace branch is live.
    assert "recorder" not in hass.config.components

    # --- Restart: Vigil is set up while HA is still ``starting`` so it records
    # the boot anchor at the frozen restart instant, like prod.
    hass.set_state(CoreState.starting)
    hass.data.pop(DATA_BOOT_TIME, None)
    freezer.move_to(restart_instant)
    post = await _make_coordinator(
        hass, options={CONF_GRACE_PERIOD_MINUTES: 15, CONF_STARTUP_IGNORE_SECONDS: 0}
    )
    # ``_ha_started`` must be False at construction for ``_boot_time`` to be set.
    assert post._ha_started is False
    assert post._boot_time == restart_instant

    # HA finishes starting; the global startup grace lifts.
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    # Immediately after restart: boot grace defers the (stale, 1h-old) since.
    first = await post._async_update_data()
    assert first["counts"]["devices_offline"] == 0
    assert device.id in post._downtime

    # Just before grace elapses (measured from boot) — still quiet.
    freezer.move_to(restart_instant + grace - timedelta(minutes=1))
    mid = await post._async_update_data()
    assert mid["counts"]["devices_offline"] == 0

    # Past grace (~15 min after restart): now the cohort fires.
    freezer.move_to(restart_instant + grace + timedelta(minutes=1))
    late = await post._async_update_data()
    offline = {i.device_id: i for i in late["devices_offline"]}
    assert device.id in offline
    assert offline[device.id].kind is IssueKind.DEVICE_OFFLINE_CONFIRMED
    # Grace was measured from the boot anchor, not the stale pre-restart since.
    assert offline[device.id].since == restart_instant


# --- #9: integration_health friendly names override misleading titles --------


async def test_integration_health_uses_friendly_name_not_entry_title(
    hass: HomeAssistant,
) -> None:
    """integration_health shows the integration's name, not the entry title.

    Matter entries are titled after the server URL, and account integrations
    after the user's email; grouping by domain with the manifest name yields one
    "Matter" / proper-name row instead of leaking those titles.
    """
    matter = MockConfigEntry(domain="matter", title="http://matter-server.local:5580")
    matter.add_to_hass(hass)
    matter.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=matter.entry_id, identifiers={("matter", "bulb")}
    )
    light = ent_reg.async_get_or_create(
        "light", "matter", "bulb_light", device_id=device.id
    )
    hass.states.async_set(light.entity_id, "on")

    coordinator = await _make_coordinator(
        hass, options={CONF_STARTUP_IGNORE_SECONDS: 0}
    )
    data = await coordinator._async_update_data()

    rows = {row["domain"]: row for row in data["integration_health"]}
    assert "matter" in rows
    # The load-bearing behaviour: a friendly manifest name, NOT the server-URL
    # entry title. Assert that directly rather than hardcoding the exact manifest
    # string "Matter" (which couples the test to HA-core packaging).
    assert rows["matter"]["title"] != matter.title
    assert "http" not in rows["matter"]["title"]
    assert rows["matter"]["device_count"] == 1

    await _settle(hass, matter)
