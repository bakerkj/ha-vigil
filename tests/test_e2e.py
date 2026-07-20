# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""End-to-end: drive the integration through Home Assistant's real setup path.

Unlike test_init.py (which calls the entry hooks directly with the frontend
stubbed), this loads Vigil via ``hass.config_entries.async_setup`` so HA
resolves the manifest, sets up dependencies, forwards the sensor platform, runs
the first detection cycle, and registers the panel best-effort — then unloads.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import persistent_notification
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil.config_flow import DEFAULTS
from custom_components.vigil.const import (
    CONF_AVAILABILITY_IGNORED_PLATFORMS,
    CONF_ENABLE_NOTIFICATION,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_STARTUP_IGNORE_SECONDS,
    DOMAIN,
    NOTIFICATION_ID,
)
from custom_components.vigil.models import IssueKind
from tests.helpers import _add_connectivity, _failed_entry


async def test_full_setup_detects_failure_ignores_ignored_and_unloads(
    hass: HomeAssistant,
) -> None:
    # A genuinely failing integration for Vigil to find.
    _failed_entry(hass)

    # An IGNORED discovery entry — NOT_LOADED by design; must NOT be flagged.
    ignored = MockConfigEntry(domain="led_ble", title="Ignored", source=SOURCE_IGNORE)
    ignored.add_to_hass(hass)
    ignored.mock_state(hass, ConfigEntryState.NOT_LOADED)

    # Opt into the notification (off by default) — this test asserts it is raised.
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Vigil",
        data=dict(DEFAULTS),
        options={CONF_ENABLE_NOTIFICATION: True},
    )
    entry.add_to_hass(hass)

    # Real setup: manifest deps, platform forwarding, first refresh, panel reg.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # The six count sensors + the detail (state) sensor + the clear-
    # acknowledgements button are registered.
    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert len(entities) == 8
    assert "button.vigil_clear_acknowledgements" in {e.entity_id for e in entities}

    # The detection cycle ran and found the broken integration.
    total = hass.states.get("sensor.vigil_total_issues")
    assert total is not None
    assert int(total.state) >= 1
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert NOTIFICATION_ID in notifications

    # The ignored entry is NOT among the reported failures; the real one is.
    data = entry.runtime_data.coordinator.data
    failed_domains = {i.domain for i in data["integration_failures"]}
    assert "demo" in failed_domains
    assert "led_ble" not in failed_domains

    # Clean unload through HA.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Annotate to widen back from the LOADED narrowing of the assert above.
    state_after: ConfigEntryState = entry.state
    assert state_after is ConfigEntryState.NOT_LOADED
    assert DOMAIN not in hass.data


async def test_already_offline_device_is_reported(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A device that went offline before Vigil started is reported immediately.

    Reproduces the real-world ESPHome case: data entities unavailable for hours,
    an always-available `update` entity, and detection that must not wait a fresh
    grace period from install.
    """
    now = dt_util.utcnow()

    # Create the device + states two hours in the past so last_changed is old.
    freezer.move_to(now - timedelta(hours=2))
    hub = MockConfigEntry(domain="demo", title="Demo Hub")
    hub.add_to_hass(hass)
    hub.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id, identifiers={("demo", "attic")}, name="Attic Node"
    )
    data_sensor = ent_reg.async_get_or_create(
        "sensor", "demo", "attic_t", device_id=device.id
    )
    fw = ent_reg.async_get_or_create("update", "demo", "attic_fw", device_id=device.id)
    hass.states.async_set(data_sensor.entity_id, "unavailable")
    hass.states.async_set(fw.entity_id, "off")  # stays available while offline

    # Back to the present and start Vigil (startup grace off for the test).
    freezer.move_to(now)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Vigil",
        data={**DEFAULTS, CONF_STARTUP_IGNORE_SECONDS: 0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    data = entry.runtime_data.coordinator.data
    offline_names = {i.name for i in data["devices_offline"]}
    assert "Attic Node" in offline_names


async def test_realistic_mixed_install_one_setup(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """One setup exercising the production mix we actually observe.

    Built from real HASS shapes, all in a single Vigil cycle:

    * An ESPHome node that went offline hours ago. ESPHome keeps ONE config
      entry per device (title = device name) LOADED while the node is dead; its
      ``binary_sensor.*_status`` (connectivity) flips ``off``, its
      ``update`` firmware entity stays ``off``/available, and its real sensors
      go ``unavailable``. Must be flagged ``device_offline_confirmed`` and the
      update/status entities must NOT mask it.
    * A healthy ESPHome node (everything reporting) — never flagged.
    * An IGNORED discovery entry (source=ignore) — the kind that dominates a
      mature install (we saw 165) — must NEVER be flagged.
    * A annotation_notes-annotated device (another integration's device) that is
      offline: its telemetry is unavailable while the annotation_notes ``Battery
      type`` sensor stays available. annotation_notes must not mask the outage, so
      it is still flagged.
    * Grouped integration_health with friendly names: the two ESPHome entries
      collapse to a single "ESPHome" row even though their titles are device
      names.
    """
    now = dt_util.utcnow()
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    # --- Build everything two hours in the past so last_changed is genuinely
    # old (an already-offline cohort), then come back to "now" to run Vigil.
    freezer.move_to(now - timedelta(hours=2))

    # ESPHome offline node: its own config entry, title = device name, LOADED.
    esp_offline = MockConfigEntry(domain="esphome", title="Attic Sensor")
    esp_offline.add_to_hass(hass)
    esp_offline.mock_state(hass, ConfigEntryState.LOADED)
    off_device = dev_reg.async_get_or_create(
        config_entry_id=esp_offline.entry_id,
        identifiers={("esphome", "attic")},
        name="Attic Sensor",
    )
    off_temp = ent_reg.async_get_or_create(
        "sensor", "esphome", "attic_temp", device_id=off_device.id
    )
    off_rssi = ent_reg.async_get_or_create(
        "sensor",
        "esphome",
        "attic_rssi",
        device_id=off_device.id,
        original_device_class="signal_strength",
    )
    off_status = _add_connectivity(
        ent_reg, off_device, "attic_status", integration="esphome"
    )
    off_fw = ent_reg.async_get_or_create(
        "update", "esphome", "attic_fw", device_id=off_device.id
    )
    hass.states.async_set(off_temp.entity_id, "unavailable")
    hass.states.async_set(off_rssi.entity_id, "unavailable")
    hass.states.async_set(off_status.entity_id, "off")  # connectivity DOWN
    hass.states.async_set(off_fw.entity_id, "off")  # firmware entity lingers

    # ESPHome healthy node: its own config entry, all entities reporting.
    esp_healthy = MockConfigEntry(domain="esphome", title="Office Sensor")
    esp_healthy.add_to_hass(hass)
    esp_healthy.mock_state(hass, ConfigEntryState.LOADED)
    ok_device = dev_reg.async_get_or_create(
        config_entry_id=esp_healthy.entry_id,
        identifiers={("esphome", "office")},
        name="Office Sensor",
    )
    ok_temp = ent_reg.async_get_or_create(
        "sensor", "esphome", "office_temp", device_id=ok_device.id
    )
    ok_status = _add_connectivity(
        ent_reg, ok_device, "office_status", integration="esphome"
    )
    hass.states.async_set(ok_temp.entity_id, "21.5")
    hass.states.async_set(ok_status.entity_id, "on")

    # A annotation_notes-annotated device owned by another integration (mqtt),
    # offline: telemetry unavailable, the annotation sensor stays available.
    mqtt_entry = MockConfigEntry(domain="mqtt", title="MQTT")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry.mock_state(hass, ConfigEntryState.LOADED)
    door_device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        identifiers={("mqtt", "front_door")},
        name="Front Door",
    )
    door_contact = ent_reg.async_get_or_create(
        "binary_sensor", "mqtt", "front_door_contact", device_id=door_device.id
    )
    # annotation_notes annotates the SAME device with its own platform entities.
    batt_type = ent_reg.async_get_or_create(
        "sensor",
        "annotation_notes",
        "front_door_battery_type",
        device_id=door_device.id,
    )
    hass.states.async_set(door_contact.entity_id, "unavailable")
    hass.states.async_set(batt_type.entity_id, "CR2032")  # annotation, stays

    # Back to the present: the cohort has now been offline for two hours.
    freezer.move_to(now)

    # An IGNORED discovery entry — must never be flagged.
    ignored = MockConfigEntry(domain="led_ble", title="LED-1234", source=SOURCE_IGNORE)
    ignored.add_to_hass(hass)
    ignored.mock_state(hass, ConfigEntryState.NOT_LOADED)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Vigil",
        data={
            **DEFAULTS,
            CONF_STARTUP_IGNORE_SECONDS: 0,
            CONF_GRACE_PERIOD_MINUTES: 15,
            # Real install: annotation_notes configured as an ignored annotation
            # platform so it can't mask the offline annotated device.
            CONF_AVAILABILITY_IGNORED_PLATFORMS: ["annotation_notes"],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    data = entry.runtime_data.coordinator.data

    # The offline ESPHome node is flagged confirmed-offline (status said DOWN);
    # the firmware-update + status signal entities did not mask it.
    offline = {i.name: i for i in data["devices_offline"]}
    assert "Attic Sensor" in offline
    assert offline["Attic Sensor"].kind is IssueKind.DEVICE_OFFLINE_CONFIRMED
    assert offline["Attic Sensor"].domain == "esphome"

    # The healthy node is not in any issue bucket.
    assert "Office Sensor" not in offline
    assert all(i.device_id != ok_device.id for i in data["issues"])

    # The annotation_notes-annotated device is still flagged despite its lingering
    # annotation entity (no connectivity signal -> no_signal kind under grace).
    assert "Front Door" in offline
    assert offline["Front Door"].kind is IssueKind.DEVICE_OFFLINE_NO_SIGNAL

    # The ignored discovery entry is never a failure.
    assert data["counts"]["integration_failures"] == 0
    assert all(i.domain != "led_ble" for i in data["issues"])

    # integration_health is grouped by integration with friendly names: the two
    # ESPHome entries collapse into a single "ESPHome" row, not the device-name
    # titles. mqtt resolves to "MQTT".
    rows = {row["domain"]: row for row in data["integration_health"]}
    assert "esphome" in rows
    esp_row = rows["esphome"]
    assert esp_row["title"] == "ESPHome"
    assert esp_row["title"] not in ("Attic Sensor", "Office Sensor")
    assert esp_row["device_count"] == 2
    assert esp_row["offline_count"] == 1
    assert esp_row["healthy"] is False
    assert rows["mqtt"]["title"] == "MQTT"
    assert rows["mqtt"]["offline_count"] == 1
    # led_ble (ignored) contributes no health row.
    assert "led_ble" not in rows

    # These mock entries are never really set up (only mocked LOADED); drop them
    # so the hass fixture's teardown unload doesn't try to import their real
    # components (whose third-party deps aren't installed in the test env).
    for mock_entry in (esp_offline, esp_healthy, mqtt_entry):
        mock_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_all_cards_register_as_one_lovelace_resource(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: every Vigil card loads via ONE Lovelace resource (storage mode).

    The frontend's resource loader awaits each resource module's custom-element
    definition before rendering a view, so a panel view whose only card is
    ``custom:vigil-card`` has the element defined in time — closing the load race
    that otherwise shows the whole view as "Configuration error". All four card
    types live in the single ``vigil-card.js`` module, so the one registered
    resource defines them all.
    """
    from pathlib import Path

    from homeassistant.setup import async_setup_component

    import custom_components.vigil as vigil_pkg
    from custom_components.vigil import _async_register_card_resource
    from custom_components.vigil.const import CARD_FILENAME, STATIC_PATH

    assert await async_setup_component(hass, "lovelace", {})
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain=DOMAIN, title="Vigil", data=dict(DEFAULTS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    resources = hass.data["lovelace"].resources

    def _card_items() -> list[dict[str, object]]:
        return [
            item
            for item in resources.async_items()
            if CARD_FILENAME in str(item.get("url", ""))
        ]

    items = _card_items()
    # Exactly ONE resource (the single module that defines all four cards),
    # registered as a JS module at a cache-busted (?v=<mtime>) URL.
    assert len(items) == 1, [i.get("url") for i in resources.async_items()]
    base = f"{STATIC_PATH}/{CARD_FILENAME}"
    assert str(items[0]["url"]).startswith(f"{base}?v=")
    # Registered as a JS module (key name varies by HA version: res_type/type).
    assert items[0].get("res_type", items[0].get("type")) == "module"

    # That one module registers ALL FOUR card types, so all load via this resource.
    js = (Path(vigil_pkg.__file__).parent / "frontend" / CARD_FILENAME).read_text()
    for card_type in (
        "vigil-card",
        "vigil-summary-card",
        "vigil-integration-health-card",
        "vigil-issues-card",
    ):
        assert f'"{card_type}"' in js, card_type

    # Idempotent: re-registering the same version does not create a duplicate.
    await _async_register_card_resource(hass)
    assert len(_card_items()) == 1

    # A changed card (new mtime) UPDATES the one resource's cache-buster in place
    # rather than duplicating it — so a redeploy actually re-fetches the module.
    old_url = _card_items()[0]["url"]
    monkeypatch.setattr(vigil_pkg, "_file_version", lambda _p: 999999)
    await _async_register_card_resource(hass)
    updated = _card_items()
    assert len(updated) == 1
    assert updated[0]["url"] == f"{base}?v=999999"
    assert updated[0]["url"] != old_url
