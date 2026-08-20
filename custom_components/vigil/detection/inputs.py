# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Layer 2 — connectivity resolution and per-device tuple assembly."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from types import MappingProxyType

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
    DEFAULT_TRACKER_INTEGRATIONS,
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
    MacSource,
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
    mac_sources: Mapping[str, MacSource] = MappingProxyType({}),
    tracker_integrations: frozenset[str] = DEFAULT_TRACKER_INTEGRATIONS,
) -> list[DeviceTuple]:
    """Assemble a :class:`DeviceTuple` for every monitored device.

    ``ignore_connectivity`` are vigil.yaml selectors whose matched entities must
    NOT be treated as connectivity signals (a mislabeled
    ``device_class: connectivity`` sensor). ``mac_sources`` are vigil.yaml rules
    naming where an integration keeps a device's hardware address.
    ``tracker_integrations`` names domains whose device records are treated as
    shadow trackers of their MAC-siblings — see DEFAULT_TRACKER_INTEGRATIONS.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    key_index = _build_device_key_index(dev_reg, mac_sources)

    tuples: list[DeviceTuple] = []
    for device in dev_reg.devices.values():
        try:
            device_tuple = _build_one_tuple(
                hass,
                device,
                ent_reg,
                key_index,
                exclusions,
                ignore_connectivity,
                mac_sources,
                tracker_integrations,
            )
        except Exception:  # one bad device must not blank the cycle
            _LOGGER.exception("Vigil: failed to assess device %s", device.id)
            continue
        if device_tuple is not None:
            tuples.append(device_tuple)

    return tuples


def _build_one_tuple(
    hass: HomeAssistant,
    device: DeviceEntry,
    ent_reg: er.EntityRegistry,
    key_index: dict[tuple[str, str, str], list[DeviceEntry]],
    exclusions: ExclusionConfig,
    ignore_connectivity: Sequence[EntitySelector],
    mac_sources: Mapping[str, MacSource],
    tracker_integrations: frozenset[str],
) -> DeviceTuple | None:
    """Assemble a single device's tuple, or ``None`` if it isn't monitored."""
    if device.disabled:
        return None

    # HA 2026.8 splits a composite device into one record per config entry.
    # A non-primary split is a cosmetic duplicate of the primary — skip so a
    # single physical device isn't evaluated (and reported offline) more than
    # once. Primary is identified by composite_primary_config_entry.
    composite = getattr(device, "composite_device_id", None)
    primary_entry = getattr(device, "composite_primary_config_entry", None)
    if (
        composite is not None
        and primary_entry is not None
        and device.config_entry_id != primary_entry
    ):
        return None

    # Tracker/monitor integrations (see DEFAULT_TRACKER_INTEGRATIONS) create
    # their own MAC-sharing device records that aren't composite splits. When
    # the entity-owning primary is disabled, HA doesn't propagate disabled_by
    # to the tracker, and its connectivity sensor reads "off" (device
    # intentionally down), causing a phantom offline. Skip THIS record only
    # when it belongs to a tracker integration and a disabled non-tracker
    # MAC-sibling exists — directional by construction, so disabling a tracker
    # never suppresses the primary. Requires knowing which integration owns
    # this record, so we look it up before the composite-split skip below.
    device_domain = _device_primary_domain(hass, device)
    if device_domain in tracker_integrations:
        for key in _device_keys(device, mac_sources, macs_only=True):
            for sibling in key_index.get(key, []):
                if sibling.id == device.id or not sibling.disabled:
                    continue
                if _device_primary_domain(hass, sibling) not in tracker_integrations:
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
        hass,
        device,
        reg_entries,
        ent_reg,
        key_index,
        ignore_connectivity,
        exclusions,
        mac_sources,
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
def _address_keys(
    device: DeviceEntry, mac_sources: Mapping[str, MacSource]
) -> list[tuple[str, str, str]]:
    """``("mac", <address>, "")`` for every hardware address ``device`` carries.

    The standard ``mac`` connection needs no configuration. Anything else comes
    from a declared ``mac_sources`` rule: a connection type the integration
    invented, or a pattern pulling the address out of an identifier — some
    integrations publish no connection at all and hide it in a device uuid.

    Addresses are normalised to bare lowercase hex so the same hardware matches
    however each integration chose to punctuate it.
    """
    found = {
        _normalise_address(value)
        for conn_type, value in device.connections
        if conn_type == dr.CONNECTION_NETWORK_MAC
    }
    if mac_sources:
        declared_types = {
            t for source in mac_sources.values() for t in source.connection_types
        }
        found.update(
            _normalise_address(value)
            for conn_type, value in device.connections
            if conn_type in declared_types
        )
        for ident in device.identifiers:
            if len(ident) < 2:
                continue
            source = mac_sources.get(ident[0])
            if source is None or source.identifier_regex is None:
                continue
            found.update(
                _normalise_address(m)
                for m in source.identifier_regex.findall(str(ident[1]))
            )
    return [("mac", address, "") for address in found if address]


def _normalise_address(value: str) -> str:
    """A hardware address as bare lowercase hex, however it was punctuated."""
    return value.lower().replace(":", "").replace("-", "").replace(".", "")


@callback
def _drop_hub_chains(
    index: dict[tuple[str, str, str], list[DeviceEntry]],
    dev_reg: dr.DeviceRegistry,
) -> dict[tuple[str, str, str], list[DeviceEntry]]:
    """Discard keys shared by a device and something below it in a hub chain.

    A gateway's children commonly inherit its address, so a key spanning a
    ``via_device`` ancestry is not one physical thing seen twice — it is a hub
    and the things behind it. Treating those as siblings would let a child's
    signal vouch for the parent, which is exactly the false "it's up" this
    whole path must not produce. Rare enough to drop the key outright rather
    than try to pick a winner.
    """
    for key, devices in list(index.items()):
        if len(devices) < 2 or not _shares_a_hub_chain(devices, dev_reg):
            continue
        _LOGGER.debug(
            "Vigil: ignoring %s shared across a via_device chain (%s)",
            key,
            ", ".join(d.name or d.id for d in devices),
        )
        del index[key]
    return index


def _shares_a_hub_chain(devices: list[DeviceEntry], dev_reg: dr.DeviceRegistry) -> bool:
    """Whether any device in the group is an ancestor of another."""
    ids = {d.id for d in devices}
    for device in devices:
        seen: set[str] = set()
        parent = device.via_device_id
        while parent is not None and parent not in seen:
            if parent in ids:
                return True
            seen.add(parent)
            entry = dev_reg.async_get(parent)
            parent = entry.via_device_id if entry else None
    return False


@callback
def _identifier_keys(device: DeviceEntry) -> list[tuple[str, str, str]]:
    """``("id", domain, ident)`` for each of ``device``'s identifiers.

    Typed ``tuple[str, str]``, but nothing enforces that and real registries
    hold other shapes — homekit writes 3-tuples, weatherflow_forecast a
    1-tuple. Pair on the first two elements; anything shorter cannot pair and
    is skipped.
    """
    return [
        ("id", ident[0], ident[1]) for ident in device.identifiers if len(ident) >= 2
    ]


@callback
def _build_device_key_index(
    dev_reg: dr.DeviceRegistry,
    mac_sources: Mapping[str, MacSource] = MappingProxyType({}),
) -> dict[tuple[str, str, str], list[DeviceEntry]]:
    """Map each registry key to the devices carrying it.

    Keys are the two things the device registry itself matches devices on: a
    connection (``("mac", <addr>, "")``) and an identifier
    (``("id", <domain>, <ident>)``). Tagged by kind so callers can ask for one
    or both — the router-tracker path wants MACs only, while rejoining the
    halves of a split device wants either.

    ``mac_sources`` adds the addresses an integration keeps somewhere other
    than a standard ``mac`` connection, as declared in vigil.yaml.
    """
    index: dict[tuple[str, str, str], list[DeviceEntry]] = {}
    for device in dev_reg.devices.values():
        for key in _address_keys(device, mac_sources):
            index.setdefault(key, []).append(device)
        for key in _identifier_keys(device):
            index.setdefault(key, []).append(device)
    return _drop_hub_chains(index, dev_reg)


@callback
def _device_keys(
    device: DeviceEntry,
    mac_sources: Mapping[str, MacSource] = MappingProxyType({}),
    *,
    macs_only: bool = False,
) -> list[tuple[str, str, str]]:
    """The index keys ``device`` carries, in the same shape as the index."""
    keys = _address_keys(device, mac_sources)
    if not macs_only:
        keys += _identifier_keys(device)
    return keys


@callback
def _device_primary_domain(hass: HomeAssistant, device: DeviceEntry) -> str | None:
    """The domain of a device's HA-declared primary_config_entry, or None."""
    if device.primary_config_entry is None:
        return None
    entry = hass.config_entries.async_get_entry(device.primary_config_entry)
    return entry.domain if entry is not None else None


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
    key_index: dict[tuple[str, str, str], list[DeviceEntry]],
    ignore_connectivity: Sequence[EntitySelector],
    exclusions: ExclusionConfig,
    mac_sources: Mapping[str, MacSource],
) -> tuple[ConnectivityState, str]:
    """Resolve a device's reachability via the brief's priority ladder."""
    # P1/P2 look at the device's own entities plus those of its split siblings —
    # see _same_device_entries.
    entries = _same_device_entries(
        device, reg_entries, ent_reg, key_index, exclusions, mac_sources
    )
    # Entities a vigil.yaml ignore rule marks as NOT a connectivity signal (e.g. a
    # mislabeled device_class=connectivity sensor) — skipped by P1/P2 below.
    ignored = _ignored_connectivity_ids(hass, entries, ignore_connectivity)

    # Priority 1 — same-device connectivity binary_sensor (zero-config).
    result = _from_connectivity_binary_sensor(hass, entries, ignored)
    if result is not None:
        return result

    # Priority 2 — protocol-native status entity (zwave_js node_status).
    result = _from_zwave_node_status(hass, entries, ignored)
    if result is not None:
        return result

    # Priorities 4-6 — MAC correlation to a router/switch/scanner device_tracker.
    result = _from_mac_tracker(hass, device, ent_reg, key_index, mac_sources)
    if result is not None:
        return result

    # Priority 8 — no signal found.
    return ConnectivityState.UNKNOWN, "none"


def _same_device_entries(
    device: DeviceEntry,
    reg_entries: list[RegistryEntry],
    ent_reg: er.EntityRegistry,
    key_index: dict[tuple[str, str, str], list[DeviceEntry]],
    exclusions: ExclusionConfig,
    mac_sources: Mapping[str, MacSource],
) -> list[RegistryEntry]:
    """The device's own entities, followed by those of its split siblings.

    A device is one config entry's view of a physical thing. Before HA 2026.8
    the registry merged those views, so a monitoring integration's ping
    binary_sensor landed on the same device as the telemetry it watches. 2026.8
    gives each integration its own device, splitting the pair apart.

    Rejoining them keeps P1/P2 seeing the signals they saw pre-split. Without
    it, a device whose telemetry has gone quiet loses the proof it is still
    reachable, and Vigil reports an outage that isn't happening.

    Siblings are found by shared identifier *or* connection, which is complete
    by construction: those are the only two keys ``async_get_or_create`` ever
    merged on, so an integration whose signal used to sit on this device must
    publish one of them. Matching both therefore rejoins exactly the set that
    used to merge, whatever those integrations do next. Matching only on MAC
    would miss every device whose integrations publish no MAC — locally that is
    UPSs, switches, printers and similar, where the monitor copies the target's
    identifiers but there is no hardware address to share.

    Both keys are safe for the same reason: pre-split the registry enforces
    global uniqueness on each, so two devices sharing one are necessarily halves
    of a single physical device.

    This is deliberately *not* keyed on ``composite_device_id`` (the split
    marker HA stamps during migration): that only covers devices which existed
    at migration time, so a device paired with a monitor afterwards would never
    match, and HA drops the field about a year on. Identifiers and connections
    hold in both cases.

    Own entities come first so a device's own signal still wins. Note the
    sibling is a different *registry* device, not a different physical one — a
    third party observing this device from outside (a router's client tracker)
    carries its own keys, so it is not a sibling and stays on the weaker P4-6
    path where it belongs.

    Sibling entities are filtered by the entity-level exclusions exactly as the
    device's own are (see ``_build_one_tuple``), so "ignore this entity" holds
    wherever the entity lives. Device-level exclusions deliberately do *not*
    apply: excluding a device says "don't report on it", not "distrust it". The
    split leaves each monitor as a device in its own right, and excluding those
    from monitoring is the obvious tidy-up — if that also silenced their ping
    sensors it would reinstate exactly the false outages this rejoin prevents.
    To drop a specific signal, use the ``ignore_connectivity`` rules, which
    exist for that and are applied to this whole list.
    """
    keys = _device_keys(device, mac_sources)
    if not keys:
        return reg_entries

    seen = {device.id}
    sibling_entries: list[RegistryEntry] = []
    for key in keys:
        for sibling in key_index.get(key, []):
            if sibling.id in seen or sibling.disabled:
                continue
            seen.add(sibling.id)
            sibling_entries.extend(
                e
                for e in er.async_entries_for_device(
                    ent_reg, sibling.id, include_disabled_entities=False
                )
                if e.entity_id not in exclusions.entity_ids
                and _entity_domain(e.entity_id) not in exclusions.domains
            )
    if not sibling_entries:
        return reg_entries
    return [*reg_entries, *sibling_entries]


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
    key_index: dict[tuple[str, str, str], list[DeviceEntry]],
    mac_sources: Mapping[str, MacSource],
) -> tuple[ConnectivityState, str] | None:
    # MAC keys only. This priority is about a router seeing a hardware address
    # on the network; a shared identifier says two integrations describe the
    # same device, which is not evidence anything can reach it.
    for key in _device_keys(device, mac_sources, macs_only=True):
        # Candidates include the device itself: before HA 2026.8 the registry
        # merged devices sharing a MAC (so a router's client device_tracker often
        # landed on the same device); from 2026.8 they are separate devices, and
        # both shapes are matched here.
        for candidate in key_index.get(key, []):
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
