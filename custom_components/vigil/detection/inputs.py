# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Layer 2 — connectivity resolution and per-device tuple assembly."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    STATE_HOME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity_registry import RegistryEntry

from ..const import (
    AVAILABILITY_IGNORED_DOMAINS,
    DOMAIN,
    MAC_ROUTER_PLATFORMS,
    NO_VALUE_STATES,
    ZWAVE_NODE_STATUS_SUFFIX,
    ZWAVE_STATUS_DOWN,
    ZWAVE_STATUS_UNKNOWN,
    ZWAVE_STATUS_UP,
)
from ..models import (
    ConnectivityState,
    DeviceTuple,
    ExclusionConfig,
    is_device_excluded,
    resolved_device_class,
)
from ..selectors import EntitySelector

_LOGGER = logging.getLogger(__name__)

# device_tracker "present" states that count as UP.
_TRACKER_HOME = {STATE_HOME, "on"}
_TRACKER_AWAY = {"not_home", "away", STATE_OFF}


@callback
def build_device_tuples(
    hass: HomeAssistant,
    exclusions: ExclusionConfig,
    *,
    ignore_connectivity: Sequence[EntitySelector] = (),
) -> list[DeviceTuple]:
    """Assemble a :class:`DeviceTuple` for every monitored device.

    ``ignore_connectivity`` are vigil.yaml selectors whose matched entities must
    NOT be treated as connectivity signals (a mislabeled
    ``device_class: connectivity`` sensor).
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    mac_index = _build_mac_index(dev_reg)

    tuples: list[DeviceTuple] = []
    for device in dev_reg.devices.values():
        try:
            device_tuple = _build_one_tuple(
                hass, device, ent_reg, mac_index, exclusions, ignore_connectivity
            )
        except Exception:  # noqa: BLE001 — one bad device must not blank the cycle
            _LOGGER.exception("Vigil: failed to assess device %s", device.id)
            continue
        if device_tuple is not None:
            tuples.append(device_tuple)

    return tuples


def _build_one_tuple(
    hass: HomeAssistant,
    device: DeviceEntry,
    ent_reg: er.EntityRegistry,
    mac_index: dict[str, list[DeviceEntry]],
    exclusions: ExclusionConfig,
    ignore_connectivity: Sequence[EntitySelector],
) -> DeviceTuple | None:
    """Assemble a single device's tuple, or ``None`` if it isn't monitored."""
    if device.disabled:
        return None

    reg_entries = [
        e
        for e in er.async_entries_for_device(
            ent_reg, device.id, include_disabled_entities=False
        )
        if e.entity_id not in exclusions.entity_ids
        and _entity_domain(e.entity_id) not in exclusions.domains
    ]

    entry = _primary_config_entry(
        hass, device, reg_entries, exclusions.ignored_platforms
    )
    entry_domain = entry.domain if entry else None
    # Never monitor Vigil's own hub device.
    if entry_domain == DOMAIN:
        return None
    if is_device_excluded(device.id, entry_domain, exclusions):
        return None

    states = _entity_states(hass, reg_entries)
    if not states:
        return None

    connectivity_state, source = _resolve_connectivity(
        hass, device, reg_entries, ent_reg, mac_index, ignore_connectivity
    )

    # Availability is judged over the device's own telemetry, excluding the
    # connectivity/status signal, update/button domains, and annotation-platform
    # entities that stay available regardless of reachability.
    signal_ids = _signal_entity_ids(reg_entries)
    annotation_ids = {
        e.entity_id for e in reg_entries if e.platform in exclusions.ignored_platforms
    }
    data_states = [
        s
        for s in states
        if s.entity_id not in signal_ids
        and s.entity_id not in annotation_ids
        and _entity_domain(s.entity_id) not in AVAILABILITY_IGNORED_DOMAINS
    ]
    has_data = bool(data_states)
    # A data entity that is ``unavailable`` OR ``unknown`` counts as "not reporting".
    not_reporting = [s for s in data_states if s.state in NO_VALUE_STATES]
    # Most recent transition into not-reporting — the outage start.
    offline_since = max((s.last_changed for s in not_reporting), default=None)

    all_not_reporting = has_data and len(not_reporting) == len(data_states)
    # A STRONG same-device UP signal (the device's own connectivity binary_sensor
    # or Z-Wave node status) vetoes an all-``unknown`` set with nothing actually
    # ``unavailable``: that's proof the device is reachable. A shared-MAC router
    # tracker is deliberately excluded — the router seeing the MAC is weak evidence
    # that the device's own integration is alive, so it must not veto an outage.
    if (
        all_not_reporting
        and connectivity_state == ConnectivityState.UP
        and source in ("connectivity_binary_sensor", "zwave_node_status")
        and not any(s.state == STATE_UNAVAILABLE for s in not_reporting)
    ):
        all_not_reporting = False

    return DeviceTuple(
        device_id=device.id,
        device_name=device.name_by_user or device.name or "Unknown device",
        config_entry_id=entry.entry_id if entry else None,
        config_entry_domain=entry_domain,
        config_entry_title=entry.title if entry else None,
        config_entry_state=entry.state if entry else None,
        connectivity_state=connectivity_state,
        connectivity_source=source,
        entity_states=states,
        all_unavailable=all_not_reporting,
        any_unavailable=bool(not_reporting),
        is_battery=_is_battery_device(reg_entries, states),
        signal_entity_ids=signal_ids,
        data_entity_ids={s.entity_id for s in data_states},
        offline_since=offline_since,
    )


@callback
def _build_mac_index(dev_reg: dr.DeviceRegistry) -> dict[str, list[DeviceEntry]]:
    """Map each MAC to the devices that advertise it (brief priority 4-6)."""
    index: dict[str, list[DeviceEntry]] = {}
    for device in dev_reg.devices.values():
        for conn_type, value in device.connections:
            if conn_type == dr.CONNECTION_NETWORK_MAC:
                index.setdefault(value, []).append(device)
    return index


def _primary_config_entry(
    hass: HomeAssistant,
    device: DeviceEntry,
    reg_entries: list[er.RegistryEntry],
    ignored_platforms: frozenset[str],
) -> ConfigEntry | None:
    """The config entry a device is primarily attributed to — its real "home".

    Trusts HA's ``primary_config_entry`` unless it names an annotation platform;
    otherwise the config entry owning the most non-annotation entities wins.
    """
    ha_primary = device.primary_config_entry
    ha_entry = (
        hass.config_entries.async_get_entry(ha_primary)
        if ha_primary is not None
        else None
    )
    if ha_entry is not None and ha_entry.domain not in ignored_platforms:
        return ha_entry

    owners = Counter(
        e.config_entry_id
        for e in reg_entries
        if e.config_entry_id is not None and e.platform not in ignored_platforms
    )
    if owners:
        # Heaviest non-annotation owner wins; sorted() tiebreak keeps it stable.
        return hass.config_entries.async_get_entry(
            max(sorted(owners), key=owners.__getitem__)
        )
    # Prefer any non-annotation entry the device is linked to; only an entirely
    # annotation device stays attributed to that platform.
    non_annotation = []
    for eid in device.config_entries:
        entry = hass.config_entries.async_get_entry(eid)
        if entry is not None and entry.domain not in ignored_platforms:
            non_annotation.append(eid)
    non_annotation.sort()
    entry_id = (
        non_annotation[0]
        if non_annotation
        else (ha_primary or next(iter(sorted(device.config_entries)), None))
    )
    if entry_id is None:
        return None
    return hass.config_entries.async_get_entry(entry_id)


def _entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _is_connectivity_binary_sensor(entry: RegistryEntry) -> bool:
    return (
        _entity_domain(entry.entity_id) == "binary_sensor"
        and resolved_device_class(entry) == BinarySensorDeviceClass.CONNECTIVITY
    )


def _is_zwave_node_status(entry: RegistryEntry) -> bool:
    return entry.platform == "zwave_js" and (entry.unique_id or "").endswith(
        ZWAVE_NODE_STATUS_SUFFIX
    )


def _is_mac_router_tracker(entry: RegistryEntry) -> bool:
    return (
        _entity_domain(entry.entity_id) == "device_tracker"
        and entry.platform in MAC_ROUTER_PLATFORMS
    )


def _signal_entity_ids(reg_entries: list[RegistryEntry]) -> set[str]:
    """Entity ids that are connectivity/status meta-signals, not device data.

    A connectivity-class binary_sensor, a zwave_js ``node_status`` sensor, or a
    router/AP ``device_tracker`` matched by shared MAC — excluded from the
    all/any-unavailable judgement so reachability can't mask silent telemetry.
    """
    return {
        entry.entity_id
        for entry in reg_entries
        if _is_connectivity_binary_sensor(entry)
        or _is_zwave_node_status(entry)
        or _is_mac_router_tracker(entry)
    }


def _entity_states(
    hass: HomeAssistant, reg_entries: list[RegistryEntry]
) -> list[State]:
    return [
        state
        for entry in reg_entries
        if (state := hass.states.get(entry.entity_id)) is not None
    ]


def _is_battery_device(reg_entries: list[RegistryEntry], states: list[State]) -> bool:
    """Whether the device looks battery-powered (gets an extended grace)."""
    if any(resolved_device_class(entry) == "battery" for entry in reg_entries):
        return True
    return any(s.attributes.get(ATTR_DEVICE_CLASS) == "battery" for s in states)


def _resolve_connectivity(
    hass: HomeAssistant,
    device: DeviceEntry,
    reg_entries: list[RegistryEntry],
    ent_reg: er.EntityRegistry,
    mac_index: dict[str, list[DeviceEntry]],
    ignore_connectivity: Sequence[EntitySelector],
) -> tuple[ConnectivityState, str]:
    """Resolve a device's reachability via the brief's priority ladder."""
    # Entities a vigil.yaml ignore rule marks as NOT a connectivity signal (e.g. a
    # mislabeled device_class=connectivity sensor) — skipped by P1/P2 below.
    ignored = _ignored_connectivity_ids(hass, reg_entries, ignore_connectivity)

    # Priority 1 — same-device connectivity binary_sensor (zero-config).
    result = _from_connectivity_binary_sensor(hass, reg_entries, ignored)
    if result is not None:
        return result

    # Priority 2 — protocol-native status entity (zwave_js node_status).
    result = _from_zwave_node_status(hass, reg_entries, ignored)
    if result is not None:
        return result

    # Priorities 4-6 — MAC correlation to a router/switch/scanner device_tracker.
    result = _from_mac_tracker(hass, device, ent_reg, mac_index)
    if result is not None:
        return result

    # Priority 8 — no signal found.
    return ConnectivityState.UNKNOWN, "none"


def _ignored_connectivity_ids(
    hass: HomeAssistant,
    reg_entries: list[RegistryEntry],
    ignore_connectivity: Sequence[EntitySelector],
) -> frozenset[str]:
    """Entity ids a vigil.yaml ignore rule marks as NOT a connectivity signal."""
    if not ignore_connectivity:
        return frozenset()
    return frozenset(
        entry.entity_id
        for entry in reg_entries
        if any(
            sel.matches(entry, hass.states.get(entry.entity_id))
            for sel in ignore_connectivity
        )
    )


def _from_connectivity_binary_sensor(
    hass: HomeAssistant, reg_entries: list[RegistryEntry], ignored: frozenset[str]
) -> tuple[ConnectivityState, str] | None:
    for entry in reg_entries:
        if entry.entity_id in ignored or not _is_connectivity_binary_sensor(entry):
            continue
        state = hass.states.get(entry.entity_id)
        if state is None or state.state in NO_VALUE_STATES:
            continue
        if state.state == STATE_ON:
            return ConnectivityState.UP, "connectivity_binary_sensor"
        if state.state == STATE_OFF:
            return ConnectivityState.DOWN, "connectivity_binary_sensor"
    return None


def _from_zwave_node_status(
    hass: HomeAssistant, reg_entries: list[RegistryEntry], ignored: frozenset[str]
) -> tuple[ConnectivityState, str] | None:
    for entry in reg_entries:
        if entry.entity_id in ignored or not _is_zwave_node_status(entry):
            continue
        state = hass.states.get(entry.entity_id)
        if state is None:
            continue
        value = state.state
        if value in ZWAVE_STATUS_DOWN:
            return ConnectivityState.DOWN, "zwave_node_status"
        if value in ZWAVE_STATUS_UNKNOWN:
            # Sleeping battery node — reachable later, don't claim DOWN.
            return ConnectivityState.UNKNOWN, "zwave_node_status_asleep"
        if value in ZWAVE_STATUS_UP:
            return ConnectivityState.UP, "zwave_node_status"
    return None


def _from_mac_tracker(
    hass: HomeAssistant,
    device: DeviceEntry,
    ent_reg: er.EntityRegistry,
    mac_index: dict[str, list[DeviceEntry]],
) -> tuple[ConnectivityState, str] | None:
    macs = {
        value
        for conn_type, value in device.connections
        if conn_type == dr.CONNECTION_NETWORK_MAC
    }
    for mac in macs:
        # Candidates include the device itself: HA merges devices that share a
        # MAC into one entry (so a router's client device_tracker often lands on
        # the same device), but separate devices sharing a MAC are also matched.
        for candidate in mac_index.get(mac, []):
            for entry in er.async_entries_for_device(
                ent_reg, candidate.id, include_disabled_entities=False
            ):
                if not _is_mac_router_tracker(entry):
                    continue
                state = hass.states.get(entry.entity_id)
                if state is None or state.state in NO_VALUE_STATES:
                    continue
                source = f"mac:{entry.platform}"
                if state.state in _TRACKER_HOME:
                    return ConnectivityState.UP, source
                if state.state in _TRACKER_AWAY:
                    return ConnectivityState.DOWN, source
    return None
