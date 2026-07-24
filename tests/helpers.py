# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Shared test helpers.

Small fixtures that were copy-pasted verbatim across several test modules — kept
here so a fix (e.g. to the teardown flush) applies everywhere at once instead of
drifting between copies. ``_make_coordinator`` is intentionally NOT shared: its
copies genuinely diverge (different option handling per suite).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.vigil.const import (
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_ENABLE_NOTIFICATION,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_STARTUP_IGNORE_SECONDS,
    DOMAIN,
)
from custom_components.vigil.coordinator import VigilCoordinator
from custom_components.vigil.learning.interval_learner import IntervalLearner
from custom_components.vigil.models import (
    ConnectivityState,
    DeviceTuple,
    ExclusionConfig,
)


def _exclude(
    *,
    domains: Iterable[str] = (),
    entity_ids: Iterable[str] = (),
    device_ids: Iterable[str] = (),
    integrations: Iterable[str] = (),
    ignored_platforms: Iterable[str] = (),
    apps: Iterable[str] = (),
) -> ExclusionConfig:
    """An ExclusionConfig from any subset of the exclusion lists (each defaulting
    to empty) — the general form of the per-suite exclusion builders."""
    return ExclusionConfig(
        domains=frozenset(domains),
        entity_ids=frozenset(entity_ids),
        device_ids=frozenset(device_ids),
        integrations=frozenset(integrations),
        ignored_platforms=frozenset(ignored_platforms),
        apps=frozenset(apps),
    )


# An ExclusionConfig that excludes nothing (the common baseline).
NO_EXCLUSIONS = _exclude()


def seed_learner(
    learner: IntervalLearner,
    entity_id: str,
    *,
    gap_seconds: float,
    days: int,
    epoch: datetime | None = None,
) -> None:
    """Seed the learner with a `gap_seconds` longest-gap bucket on each of `days`
    consecutive days, via the public ingest() seam (not private attributes)."""
    epoch = epoch or datetime(2026, 6, 1, tzinfo=UTC)
    buckets = {
        (entity_id, (epoch + timedelta(days=d)).toordinal()): gap_seconds
        for d in range(days)
    }
    learner.ingest(
        buckets,
        {entity_id: epoch + timedelta(days=days)},
        epoch + timedelta(days=days),
    )


def _entry(hass: HomeAssistant, domain: str, title: str) -> MockConfigEntry:
    """A LOADED mock config entry, added to hass."""
    entry = MockConfigEntry(domain=domain, title=title)
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


def _failed_entry(
    hass: HomeAssistant, title: str = "Demo", domain: str = "demo"
) -> MockConfigEntry:
    """A SETUP_RETRY mock config entry, added to hass — the failed-setup shape."""
    entry = MockConfigEntry(domain=domain, title=title)
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    return entry


def _demo_device(
    hass: HomeAssistant, slug: str, *, domain: str = "demo", title: str = "Demo"
) -> tuple[MockConfigEntry, er.EntityRegistry, dr.DeviceEntry]:
    """A LOADED 'demo' entry plus a bare device on it (no entities attached).

    Returns ``(entry, ent_reg, device)`` so connectivity tests can hang their own
    bespoke entities (connectivity/battery/zwave) off the device.
    """
    entry = _entry(hass, domain, title)
    ent_reg = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(domain, slug)}
    )
    return entry, ent_reg, device


async def _settle(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    """Quiesce a test that uses config-entry domains with uninstalled deps.

    Marks each mocked entry NOT_LOADED so the ``hass`` fixture's teardown doesn't
    try to import the real component to unload it, and fires the final-write event
    so debounced ``Store`` saves (entity registry, the interval learner) flush
    instead of lingering as timers past teardown.
    """
    for entry in entries:
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


async def _make_coordinator(
    hass: HomeAssistant, options: dict[str, object] | None = None
) -> VigilCoordinator:
    """A VigilCoordinator wired to a fresh single-instance entry + loaded learner.

    (Suites whose coordinator setup genuinely diverges — the ground-truth ignored-
    platform defaults, the boot-anchor timing, the floor-test option merge — keep
    their own inline construction.)

    The persistent notification is enabled here by default so the notifier tests
    stay meaningful; the *production* default is OFF (see
    ``test_notification_off_by_default``). Pass ``CONF_ENABLE_NOTIFICATION`` to
    override.
    """
    merged = {CONF_ENABLE_NOTIFICATION: True, **(options or {})}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged)
    entry.add_to_hass(hass)
    learner = IntervalLearner(hass)
    await learner.async_load()
    return VigilCoordinator(hass, entry, learner)


async def _make_device(
    hass: HomeAssistant, ident: str, n_sensors: int = 1
) -> tuple[str, list[str]]:
    """A loadable 'demo' hub carrying a device with ``n_sensors`` data sensors.

    Returns ``(device_id, entity_ids)``. The sensor names are opaque handles — only
    their identity (rows in the recorder / registry) matters to the callers. The
    'demo' domain is used so the ``hass`` fixture teardown can unload cleanly.
    """
    hub = MockConfigEntry(domain="demo", title=f"Hub {ident}")
    hub.add_to_hass(hass)
    hub.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id, identifiers={("demo", ident)}
    )
    eids: list[str] = []
    for i in range(n_sensors):
        ent = ent_reg.async_get_or_create(
            "sensor", "demo", f"{ident}_s{i}", device_id=device.id
        )
        eids.append(ent.entity_id)
    return device.id, eids


def _offline_device(
    hass: HomeAssistant, config_entry: MockConfigEntry, slug: str
) -> tuple[dr.DeviceEntry, str, str]:
    """A device whose only data sensor is ``unavailable`` and whose connectivity
    binary_sensor reports ``off`` — the confirmed-offline shape. Returns
    ``(device, data_sensor_id, connectivity_id)`` so callers can drive recovery."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("demo", slug)}
    )
    data_sensor = ent_reg.async_get_or_create(
        "sensor", "demo", f"{slug}s", device_id=device.id
    )
    conn = ent_reg.async_get_or_create(
        "binary_sensor",
        "demo",
        f"{slug}c",
        device_id=device.id,
        original_device_class="connectivity",
    )
    hass.states.async_set(data_sensor.entity_id, "unavailable")
    hass.states.async_set(conn.entity_id, "off")
    return device, data_sensor.entity_id, conn.entity_id


async def _signal_only_device(hass: HomeAssistant, ident: str) -> tuple[str, str]:
    """A device whose ONLY entity is a connectivity binary_sensor (the signal-only
    ping-host shape). Returns ``(device_id, connectivity_entity_id)``; the caller
    sets the connectivity state and its timing."""
    hub = MockConfigEntry(domain="demo", title="Ping Hub")
    hub.add_to_hass(hass)
    hub.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id, identifiers={("demo", ident)}
    )
    conn = ent_reg.async_get_or_create(
        "binary_sensor",
        "demo",
        f"{ident}_conn",
        device_id=device.id,
        original_device_class="connectivity",
    )
    return device.id, conn.entity_id


async def _record(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entity_id: str,
    value: str,
    *,
    at: datetime,
) -> None:
    """Move the clock to ``at``, set ``entity_id`` to ``value``, and flush the
    recorder — the record-a-state-at-time-T triple the recorder suites repeat."""
    freezer.move_to(at)
    hass.states.async_set(entity_id, value)
    await async_wait_recording_done(hass)


def _add_connectivity(
    ent_reg: er.EntityRegistry,
    device: dr.DeviceEntry,
    uid: str,
    *,
    integration: str = "demo",
) -> er.RegistryEntry:
    """A ``device_class: connectivity`` binary_sensor on ``device`` (the zero-config
    P1 signal). Returns the registry entry so callers can set its state/timing."""
    return ent_reg.async_get_or_create(
        "binary_sensor",
        integration,
        uid,
        device_id=device.id,
        original_device_class="connectivity",
    )


def make_device_tuple(**overrides: Any) -> DeviceTuple:
    """A :class:`DeviceTuple` with every required field defaulted, overridable by
    keyword — the shared field wiring for the engine suites.

    Each suite keeps a thin local ``_tuple`` wrapper that sets its own shape
    defaults (offline vs. healthy) and derived fields, so the detection-semantic
    defaults stay visible per suite; this only removes the boilerplate field list.
    """
    fields: dict[str, Any] = {
        "device_id": "dev1",
        "device_name": "Device 1",
        "config_entry_id": "entry1",
        "config_entry_domain": "demo",
        "config_entry_title": "Demo",
        "config_entry_state": None,
        "connectivity_state": ConnectivityState.DOWN,
        "connectivity_source": "ping",
        "entity_states": [],
        "all_unavailable": True,
        "any_unavailable": True,
        "is_battery": False,
    }
    fields.update(overrides)
    return DeviceTuple(**fields)


async def _recorder_coordinator(
    hass: HomeAssistant, **opts: object
) -> VigilCoordinator:
    """A VigilCoordinator with the recorder suites' standard grace options (1-min
    grace, no startup-ignore, 1.0 battery multiplier), each overridable via ``opts``.

    (This is the recorder-aware sibling of ``_make_coordinator``: the recorder e2e
    suites all share this exact option baseline, so it lives here instead of being
    re-inlined per test.)
    """
    options: dict[str, object] = {
        CONF_GRACE_PERIOD_MINUTES: 1,
        CONF_STARTUP_IGNORE_SECONDS: 0,
        CONF_BATTERY_GRACE_MULTIPLIER: 1.0,
    }
    options.update(opts)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    learner = IntervalLearner(hass)
    await learner.async_load()
    return VigilCoordinator(hass, entry, learner)
