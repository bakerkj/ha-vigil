# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Declared address sources (``mac_sources:``) and the hub-chain guard.

Vigil reads a standard ``mac`` connection unaided. An integration that keeps
the address anywhere else is described by a rule, so an integration nobody
described never correlates — the failure mode is a link not made, not a link
made wrongly.
"""

from __future__ import annotations

import re

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.vigil.detection.inputs as conn_mod
from custom_components.vigil.detection.engines.watch_config import (
    parse_mac_sources,
    parse_vigil_config,
)
from custom_components.vigil.detection.inputs import build_device_tuples
from custom_components.vigil.models import (
    ConnectivityState,
    DeviceTuple,
    MacSource,
)
from tests.helpers import NO_EXCLUSIONS, _add_connectivity, _entry

_MAC = "40:2f:86:40:5e:86"
_BARE = "402f86405e86"

_ARUBA = {"apdemo": MacSource(connection_types=("apdemo_mac",))}
_THINQ = {"thinqdemo": MacSource(identifier_regex=re.compile(r"-([0-9a-f]{12})$"))}


def _device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    identifiers: set[tuple[str, str]] | None = None,
    connections: set[tuple[str, str]] | None = None,
    name: str = "d",
) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=identifiers or set(),
        connections=connections or set(),
        name=name,
    )


# ── reading declared sources ─────────────────────────────────────────────────


async def test_declared_connection_type_is_read_as_an_address(
    hass: HomeAssistant,
) -> None:
    """An integration's own connection type carries the address."""
    entry = _entry(hass, "apdemo", "AP")
    device = _device(
        hass,
        entry,
        identifiers={("apdemo", "e_client_402f86405e86")},
        connections={("apdemo_mac", _MAC)},
    )
    assert ("mac", _BARE, "") in conn_mod._address_keys(device, _ARUBA)
    assert ("mac", _BARE, "") not in conn_mod._address_keys(device, {})


async def test_declared_identifier_regex_extracts_the_address(
    hass: HomeAssistant,
) -> None:
    """smartthinq publishes no connection; the address is the uuid node field."""
    entry = _entry(hass, "thinqdemo", "ThinQ")
    device = _device(
        hass,
        entry,
        identifiers={("thinqdemo", "c96a575e-b493-1751-8458-402f86405e86")},
    )
    assert ("mac", _BARE, "") in conn_mod._address_keys(device, _THINQ)
    assert conn_mod._address_keys(device, {}) == []


async def test_addresses_normalise_across_punctuation(hass: HomeAssistant) -> None:
    """The same hardware matches however each integration punctuates it."""
    entry = _entry(hass, "apdemo", "AP")
    a = _device(
        hass,
        entry,
        identifiers={("apdemo", "a")},
        connections={("apdemo_mac", "40-2F-86-40-5E-86")},
    )
    b = _device(
        hass,
        entry,
        identifiers={("apdemo", "b")},
        connections={(dr.CONNECTION_NETWORK_MAC, _MAC)},
    )
    assert conn_mod._address_keys(a, _ARUBA) == conn_mod._address_keys(b, _ARUBA)


async def test_a_rule_only_applies_to_its_own_integration(hass: HomeAssistant) -> None:
    """An identifier rule must not be applied to another integration's ids."""
    entry = _entry(hass, "otherdemo", "Other")
    device = _device(
        hass, entry, identifiers={("otherdemo", "c96a575e-b493-1751-8458-402f86405e86")}
    )
    assert conn_mod._address_keys(device, _THINQ) == []


async def test_undeclared_integration_does_not_correlate(hass: HomeAssistant) -> None:
    """Fail closed: with no rule, two devices sharing a buried address are
    unrelated as far as vigil is concerned."""
    entry = _entry(hass, "thinqdemo", "ThinQ")
    a = _device(
        hass,
        entry,
        identifiers={("thinqdemo", "c96a575e-b493-1751-8458-402f86405e86")},
    )
    other = _entry(hass, "apdemo", "AP")
    b = _device(
        hass,
        other,
        identifiers={("apdemo", "e_client_402f86405e86")},
        connections={("apdemo_mac", _MAC)},
    )
    index = conn_mod._build_device_key_index(dr.async_get(hass), {})
    assert all(len(v) == 1 or {a.id, b.id} - {d.id for d in v} for v in index.values())


async def test_declared_rules_link_the_two_views(hass: HomeAssistant) -> None:
    """With both rules, the appliance and the AP's client row share a key."""
    thinq = _entry(hass, "thinqdemo", "ThinQ")
    aruba = _entry(hass, "apdemo", "AP")
    appliance = _device(
        hass,
        thinq,
        identifiers={("thinqdemo", "c96a575e-b493-1751-8458-402f86405e86")},
    )
    client = _device(
        hass,
        aruba,
        identifiers={("apdemo", "e_client_402f86405e86")},
        connections={("apdemo_mac", _MAC)},
    )
    index = conn_mod._build_device_key_index(dr.async_get(hass), {**_ARUBA, **_THINQ})
    assert {d.id for d in index[("mac", _BARE, "")]} == {appliance.id, client.id}


async def test_the_sibling_signal_is_found_through_a_declared_address(
    hass: HomeAssistant,
) -> None:
    """End to end: telemetry all unknown, but the AP's client row says it is on
    the network, so the device resolves UP rather than being reported down."""
    ent_reg = er.async_get(hass)
    thinq = _entry(hass, "thinqdemo", "ThinQ")
    aruba = _entry(hass, "apdemo", "AP")
    appliance = _device(
        hass,
        thinq,
        identifiers={("thinqdemo", "c96a575e-b493-1751-8458-402f86405e86")},
        name="Washer",
    )
    client = _device(
        hass,
        aruba,
        identifiers={("apdemo", "e_client_402f86405e86")},
        connections={("apdemo_mac", _MAC)},
        name="lg-washer",
    )
    data = ent_reg.async_get_or_create(
        "sensor", "thinqdemo", "w1", device_id=appliance.id
    )
    conn = _add_connectivity(ent_reg, client, "apconn", integration="apdemo")
    hass.states.async_set(data.entity_id, "unknown")
    hass.states.async_set(conn.entity_id, "on")

    def state_of(sources: dict[str, MacSource]) -> DeviceTuple:
        tuples = build_device_tuples(hass, NO_EXCLUSIONS, mac_sources=sources)
        return {t.device_id: t for t in tuples}[appliance.id]

    undeclared = state_of({})
    assert undeclared.connectivity_state is ConnectivityState.UNKNOWN

    declared = state_of({**_ARUBA, **_THINQ})
    assert declared.connectivity_state is ConnectivityState.UP
    assert declared.connectivity_source == "connectivity_binary_sensor"


# ── the hub-chain guard ──────────────────────────────────────────────────────


async def test_a_hub_and_its_children_are_not_siblings(hass: HomeAssistant) -> None:
    """A gateway's children commonly carry the gateway's address in their own
    ids. A rule declared for such an integration would pair a child with its
    parent, and then the child's signal would vouch for the parent — the false
    "it's up" this path must never produce.
    """
    entry = _entry(hass, "bridgedemo", "Bridge")
    reg = dr.async_get(hass)
    gateway = reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("bridgedemo", f"gw-{_BARE}")},
        name="Gateway",
    )
    child = reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("bridgedemo", f"blind-{_BARE}")},
        # via_device_id (not the deprecated via_device tuple, removed 2026.9).
        via_device_id=gateway.id,
        name="Blind",
    )
    rule = {"bridgedemo": MacSource(identifier_regex=re.compile(r"-([0-9a-f]{12})$"))}
    assert child.via_device_id == gateway.id  # the chain the guard looks for
    assert ("mac", _BARE, "") in conn_mod._address_keys(gateway, rule)
    assert ("mac", _BARE, "") not in conn_mod._build_device_key_index(reg, rule)


async def test_unrelated_devices_sharing_an_address_still_link(
    hass: HomeAssistant,
) -> None:
    """The guard must not throw away the ordinary two-views-of-one-thing case."""
    a_entry = _entry(hass, "lampdemo", "Lamp")
    b_entry = _entry(hass, "apdemo", "AP")
    a = _device(hass, a_entry, connections={(dr.CONNECTION_NETWORK_MAC, _MAC)})
    b = _device(
        hass,
        b_entry,
        identifiers={("apdemo", "e_client_402f86405e86")},
        connections={(dr.CONNECTION_NETWORK_MAC, _MAC)},
    )
    index = conn_mod._build_device_key_index(dr.async_get(hass), {})
    assert {d.id for d in index[("mac", _BARE, "")]} == {a.id, b.id}


# ── configuration ────────────────────────────────────────────────────────────


def test_parses_both_rule_shapes() -> None:
    sources = parse_mac_sources(
        {
            "apdemo": {"connection_types": ["apdemo_mac"]},
            "thinqdemo": {"identifier_regex": r"-([0-9a-f]{12})$"},
        }
    )
    assert sources["apdemo"].connection_types == ("apdemo_mac",)
    assert sources["thinqdemo"].identifier_regex is not None


@pytest.mark.parametrize(
    "rule",
    [
        {"identifier_regex": "([0-9a-f]{12}"},  # unparsable
        {"identifier_regex": "[0-9a-f]{12}"},  # no capture group
        {"identifier_regex": "([0-9a-f]{2})-([0-9a-f]{2})"},  # two groups
        {},  # says nothing
    ],
)
def test_a_bad_rule_is_skipped_not_fatal(rule: dict[str, str]) -> None:
    """One unusable rule must not take the good ones with it."""
    sources = parse_mac_sources({"bad": rule, "good": {"connection_types": ["x_mac"]}})
    assert "bad" not in sources
    assert "good" in sources


def test_a_broken_section_raises_so_the_last_good_config_is_kept() -> None:
    with pytest.raises(vol.Invalid):
        parse_mac_sources(["not", "a", "mapping"])


def test_every_rule_invalid_raises_rather_than_parsing_to_nothing() -> None:
    """An empty result reads as a successful parse, which would silently
    replace working rules with none and correlate nothing at all."""
    with pytest.raises(vol.Invalid):
        parse_mac_sources({"a": {}, "b": {"identifier_regex": "([0-9a-f]{12}"}})


def test_an_empty_section_is_not_an_error() -> None:
    """Nothing declared is a valid choice, distinct from everything broken."""
    assert parse_mac_sources({}) == {}


def test_section_is_optional() -> None:
    assert parse_vigil_config({"watch": []}).mac_sources == {}
    assert parse_vigil_config(None).mac_sources == {}
