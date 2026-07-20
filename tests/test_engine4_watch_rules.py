# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Tests for Engine 4 — declarative watch rules."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil.detection.inputs import build_device_tuples
from custom_components.vigil.const import VIGIL_CONFIG_FILE, WATCH_RULES_FILE
from custom_components.vigil.models import (
    DeviceTuple,
    FaultPhase,
    IssueKind,
    VigilIssue,
)
from custom_components.vigil.detection.engines.engine4_watch_rules import (
    detect_watch_issues,
)
from custom_components.vigil.detection.engines.watch_config import (
    FaultState,
    VigilConfigStore,
    WatchRule,
    parse_ignore_rules,
    parse_vigil_config,
    parse_watch_rules,
)
from tests.helpers import NO_EXCLUSIONS


def _device_with_sensor(
    hass: HomeAssistant,
    *,
    platform: str,
    object_id: str,
    state: str,
    device_class: str | None = None,
    translation_key: str | None = None,
) -> dr.DeviceEntry:
    """A device owned by ``platform`` with a single sensor set to ``state``.

    The config entry is left NOT_LOADED so teardown doesn't import the real
    component; entry state is irrelevant to the watch engine.
    """
    entry = MockConfigEntry(domain=platform, title=platform)
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(platform, object_id)}
    )
    ent = ent_reg.async_get_or_create(
        "sensor",
        platform,
        f"{object_id}_uid",
        suggested_object_id=object_id,
        device_id=device.id,
        original_device_class=device_class,
        translation_key=translation_key,
    )
    hass.states.async_set(ent.entity_id, state)
    return device


def _tuples(hass: HomeAssistant) -> list[DeviceTuple]:
    return build_device_tuples(hass, NO_EXCLUSIONS)


def _detect(
    hass: HomeAssistant,
    rules: list[WatchRule],
    *,
    fault_state: dict[str, FaultState] | None = None,
    now: datetime | None = None,
) -> list[VigilIssue]:
    """Run Engine 4 with a fresh (or supplied) debounce state and clock."""
    return detect_watch_issues(
        hass,
        _tuples(hass),
        rules=rules,
        fault_state={} if fault_state is None else fault_state,
        now=now if now is not None else dt_util.utcnow(),
    )


ESPHOME_RULE = [
    {
        "name": "ESPHome component health",
        "integration": "esphome",
        "match": {"entity_id_glob": "sensor.*_component_*"},
        "ok_states": ["ok", "none", ""],
        "detail": "Component fault: {state}",
    }
]


# --- matching: integration AND entity ---------------------------------------


async def test_matches_integration_and_entity_flags_not_ok(
    hass: HomeAssistant,
) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="node_component_status", state="warning"
    )
    rules = parse_watch_rules(ESPHOME_RULE)
    issues = _detect(hass, rules)
    assert len(issues) == 1
    assert issues[0].kind is IssueKind.DEVICE_FAULT
    assert issues[0].detail == "Component fault: warning"
    assert issues[0].domain == "esphome"
    assert issues[0].source == "ESPHome component health"


async def test_ok_state_does_not_flag(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="node_component_status", state="ok"
    )
    issues = _detect(hass, parse_watch_rules(ESPHOME_RULE))
    assert issues == []


async def test_wrong_integration_same_entity_is_not_flagged(
    hass: HomeAssistant,
) -> None:
    """A same-named sensor from another integration must not match (matches on
    integration AND entity)."""
    _device_with_sensor(
        hass, platform="mqtt", object_id="thing_component_status", state="warning"
    )
    issues = _detect(hass, parse_watch_rules(ESPHOME_RULE))
    assert issues == [], "a non-esphome sensor must not match the esphome rule"


async def test_both_integrations_only_matching_one_flags(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="a_component_status", state="error"
    )
    _device_with_sensor(
        hass, platform="mqtt", object_id="b_component_status", state="error"
    )
    issues = _detect(hass, parse_watch_rules(ESPHOME_RULE))
    assert len(issues) == 1
    assert issues[0].detail == "Component fault: error"


# --- ok_states / case sensitivity -------------------------------------------


async def test_ok_states_case_insensitive_by_default(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="OK"
    )
    issues = _detect(hass, parse_watch_rules(ESPHOME_RULE))
    assert issues == []  # "OK" folds to "ok"


async def test_case_sensitive_option(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="OK"
    )
    rule = [{**ESPHOME_RULE[0], "case_sensitive": True, "ok_states": ["ok"]}]
    issues = _detect(hass, parse_watch_rules(rule))
    assert len(issues) == 1  # "OK" != "ok" when case-sensitive


# --- ignore_unavailable ------------------------------------------------------


async def test_ignore_unavailable_default_skips(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="unavailable"
    )
    issues = _detect(hass, parse_watch_rules(ESPHOME_RULE))
    assert issues == []  # unavailable is not a "fault", it's an offline concern


async def test_ignore_unavailable_false_flags(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="unavailable"
    )
    rule = [{**ESPHOME_RULE[0], "ignore_unavailable": False}]
    issues = _detect(hass, parse_watch_rules(rule))
    assert len(issues) == 1


# --- other matchers ----------------------------------------------------------


async def test_suffix_and_device_class_matchers(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass,
        platform="esphome",
        object_id="node_problem",
        state="bad",
        device_class="problem",
    )
    rule = [
        {
            "name": "problem class",
            "integration": "esphome",
            "match": {"entity_id_suffix": "_problem", "device_class": "problem"},
            "ok_states": ["ok"],
        }
    ]
    issues = _detect(hass, parse_watch_rules(rule))
    assert len(issues) == 1


async def test_device_class_mismatch_does_not_flag(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass,
        platform="esphome",
        object_id="node_problem",
        state="bad",
        device_class="temperature",
    )
    rule = [
        {
            "name": "problem class",
            "integration": "esphome",
            "match": {"device_class": "problem"},
        }
    ]
    issues = _detect(hass, parse_watch_rules(rule))
    assert issues == []


async def test_bad_detail_template_falls_back_not_raises(hass: HomeAssistant) -> None:
    """A schema-valid but wrong template must degrade to the literal text, never
    raise into the detection cycle."""
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="warning"
    )
    for bad in ("{state.value}", "{state[key]}", "{missing}"):
        rule = [{**ESPHOME_RULE[0], "detail": bad}]
        issues = _detect(hass, parse_watch_rules(rule))
        assert len(issues) == 1
        assert issues[0].detail == bad  # raw template, no crash


async def test_first_flagging_rule_wins_ok_verdict_does_not_mask(
    hass: HomeAssistant,
) -> None:
    """A lenient rule that considers the value OK must not short-circuit a later
    rule that flags it."""
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="degraded"
    )
    rules = parse_watch_rules(
        [
            {
                "name": "lenient",
                "integration": "esphome",
                "match": {"entity_id_glob": "sensor.*_component_*"},
                "ok_states": ["ok", "degraded"],
            },
            {
                "name": "strict",
                "integration": "esphome",
                "match": {"entity_id_glob": "sensor.*_component_*"},
                "ok_states": ["ok"],
                "detail": "flagged by strict",
            },
        ]
    )
    issues = _detect(hass, rules)
    assert len(issues) == 1
    assert issues[0].source == "strict"


# --- acknowledge identity ----------------------------------------------------


def test_fault_issue_key_is_distinct_from_device_and_per_entity() -> None:
    """A fault key differs from the device-offline key and per-sibling-entity."""
    from custom_components.vigil.models import VigilIssue, issue_key

    offline = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_CONFIRMED,
        name="D",
        integration="x",
        detail="",
        device_id="dev1",
    )
    fault_a = VigilIssue(
        kind=IssueKind.DEVICE_FAULT,
        name="D",
        integration="x",
        detail="",
        device_id="dev1",
        entity_id="sensor.dev1_comp_a",
        source="rule",
    )
    fault_b = VigilIssue(
        kind=IssueKind.DEVICE_FAULT,
        name="D",
        integration="x",
        detail="",
        device_id="dev1",
        entity_id="sensor.dev1_comp_b",
        source="rule",
    )
    assert issue_key(fault_a) != issue_key(offline)
    assert issue_key(fault_a) != issue_key(fault_b)


# --- schema validation -------------------------------------------------------


@pytest.mark.parametrize(
    "bad_input",
    [
        [{"name": "x", "match": {"entity_id_glob": "*"}}],  # no integration
        [{"name": "x", "integration": "esphome", "match": {}}],  # no match criterion
        [{"name": "x", "integration": "esphome", "match": {"bogus": "y"}}],  # unknown
        {"name": "x", "integration": "esphome"},  # not a list of rules
        [  # every rule invalid → wholesale-broken edit
            {"name": "a", "match": {"entity_id_glob": "sensor.*"}},
            {"name": "b", "match": {"entity_id_glob": "sensor.*"}},
        ],
    ],
)
def test_invalid_rules_raise(bad_input: object) -> None:
    """Structurally/wholesale-invalid input raises so the caller keeps last-good."""
    with pytest.raises(vol.Invalid):
        parse_watch_rules(bad_input)


def test_one_bad_rule_does_not_drop_the_good_ones() -> None:
    """A single malformed rule is skipped; valid rules in the same file still load."""
    rules = parse_watch_rules(
        [
            {
                "name": "good",
                "integration": "esphome",
                "match": {"entity_id_glob": "sensor.*"},
            },
            {"name": "broken", "match": {"entity_id_glob": "sensor.*"}},  # no integ.
        ]
    )
    assert [r.name for r in rules] == ["good"]


# --- VigilConfigStore ----------------------------------------------------------


def _write_rules(hass: HomeAssistant, text: str, *, mtime: float) -> str:
    path = hass.config.path(WATCH_RULES_FILE)
    Path(path).write_text(text)
    os.utime(path, (mtime, mtime))
    return path


async def test_store_absent_file_is_empty(hass: HomeAssistant) -> None:
    store = VigilConfigStore(hass)
    assert await store.async_get_watch_rules() == []


_R1 = "- name: r1\n  integration: esphome\n  match: {entity_id_glob: 'sensor.*'}\n"
_R2R3 = (
    "- name: r2\n  integration: mqtt\n  match: {entity_id_glob: 'sensor.*'}\n"
    "- name: r3\n  integration: mqtt\n  match: {entity_id_glob: 'sensor.*'}\n"
)


async def test_store_loads_and_reloads_on_change(hass: HomeAssistant) -> None:
    store = VigilConfigStore(hass)
    _write_rules(hass, _R1, mtime=1000.0)
    assert [r.name for r in await store.async_get_watch_rules()] == ["r1"]

    # Unchanged file → same rules on a repeat call (cached).
    assert [r.name for r in await store.async_get_watch_rules()] == ["r1"]

    # Newer mtime → reloaded.
    _write_rules(hass, _R2R3, mtime=2000.0)
    assert [r.name for r in await store.async_get_watch_rules()] == ["r2", "r3"]

    # Same mtime, different size → the (mtime, size) signature still reloads: an
    # edit preserving the timestamp but changing length is picked up.
    _write_rules(hass, _R1, mtime=2000.0)
    assert [r.name for r in await store.async_get_watch_rules()] == ["r1"]


async def test_store_malformed_keeps_previous_rules(hass: HomeAssistant) -> None:
    """A typo (missing required key) must not raise and must keep the last-good set."""
    store = VigilConfigStore(hass)
    _write_rules(
        hass,
        "- name: good\n  integration: esphome\n  match: {entity_id_glob: 'sensor.*'}\n",
        mtime=1000.0,
    )
    assert len(await store.async_get_watch_rules()) == 1

    _write_rules(
        hass, "- name: broken\n  match: {entity_id_glob: 'sensor.*'}\n", mtime=2000.0
    )
    rules = await store.async_get_watch_rules()
    assert len(rules) == 1 and rules[0].name == "good"


async def test_store_partial_file_keeps_valid_rules(hass: HomeAssistant) -> None:
    """On first load a mixed valid/invalid file adopts the valid rule (no last-good
    to fall back on)."""
    store = VigilConfigStore(hass)
    _write_rules(
        hass,
        "- name: good\n  integration: esphome\n  match: {entity_id_glob: 'sensor.*'}\n"
        "- name: broken\n  match: {entity_id_glob: 'sensor.*'}\n",
        mtime=1000.0,
    )
    rules = await store.async_get_watch_rules()
    assert [r.name for r in rules] == ["good"]


async def test_store_removed_file_disables_rules(hass: HomeAssistant) -> None:
    store = VigilConfigStore(hass)
    path = _write_rules(
        hass,
        "- name: r1\n  integration: esphome\n  match: {entity_id_glob: 'sensor.*'}\n",
        mtime=1000.0,
    )
    assert len(await store.async_get_watch_rules()) == 1
    os.remove(path)
    assert await store.async_get_watch_rules() == []


# --- vigil.yaml unified config (watch + ignore) ------------------------------


def test_parse_vigil_config_sections() -> None:
    """A vigil.yaml mapping parses both the watch and ignore sections."""
    cfg = parse_vigil_config(
        {
            "watch": [
                {
                    "name": "w",
                    "integration": "esphome",
                    "match": {"entity_id_glob": "sensor.*"},
                }
            ],
            "ignore": [
                {
                    "action": "connectivity",
                    "integration": "litterrobot",
                    "match": {
                        "device_class": "connectivity",
                        "entity_id_suffix": "_hopper_connected",
                    },
                }
            ],
        }
    )
    assert [r.name for r in cfg.watch_rules] == ["w"]
    assert len(cfg.ignore_connectivity) == 1
    sel = cfg.ignore_connectivity[0]
    assert sel.integration == "litterrobot"
    assert sel.entity_id_suffix == "_hopper_connected"
    assert sel.device_class == "connectivity"


def test_parse_vigil_config_legacy_list_is_watch_only() -> None:
    """A top-level list (the legacy vigil_watch.yaml shape) loads as watch rules."""
    cfg = parse_vigil_config(
        [{"name": "w", "integration": "esphome", "match": {"entity_id_glob": "s.*"}}]
    )
    assert [r.name for r in cfg.watch_rules] == ["w"]
    assert cfg.ignore_connectivity == []


def test_parse_ignore_rules_skips_invalid() -> None:
    """action + match are required; a bad action or missing piece is skipped."""
    sels = parse_ignore_rules(
        [
            {"action": "connectivity", "match": {"device_class": "connectivity"}},
            {"action": "connectivity"},  # no match -> skipped
            {"match": {"device_class": "connectivity"}},  # no action -> skipped
            {
                "action": "bogus",
                "match": {"device_class": "connectivity"},
            },  # bad action
        ]
    )
    assert len(sels) == 1
    assert sels[0].integration is None and sels[0].device_class == "connectivity"


def test_parse_ignore_rules_all_invalid_raises() -> None:
    """A wholesale-invalid ignore section raises (so the store keeps its last-good
    config) rather than silently dropping every rule — mirrors parse_watch_rules."""
    with pytest.raises(vol.Invalid):
        parse_ignore_rules(
            [
                {"action": "bogus", "match": {"device_class": "connectivity"}},
                {"action": "connectivity"},  # no match block
            ]
        )


async def test_store_prefers_vigil_yaml_with_ignore(hass: HomeAssistant) -> None:
    """vigil.yaml (with an ignore section) is read in preference to the legacy file."""
    path = hass.config.path(VIGIL_CONFIG_FILE)
    Path(path).write_text(
        "watch:\n"
        "  - name: w\n    integration: esphome\n    match: {entity_id_glob: 'sensor.*'}\n"
        "ignore:\n"
        "  - action: connectivity\n    integration: litterrobot\n"
        "    match: {device_class: connectivity, entity_id_suffix: _hopper_connected}\n"
    )
    os.utime(path, (1000.0, 1000.0))
    store = VigilConfigStore(hass)
    assert [r.name for r in await store.async_get_watch_rules()] == ["w"]
    sels = await store.async_get_ignore_connectivity()
    assert len(sels) == 1 and sels[0].integration == "litterrobot"


# --- debounce: trigger grace + clear hysteresis ------------------------------


def _lc(hass: HomeAssistant, entity_id: str) -> datetime:
    st = hass.states.get(entity_id)
    assert st is not None
    return st.last_changed


async def test_grace_seconds_delays_flag(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="warning"
    )
    rules = parse_watch_rules([{**ESPHOME_RULE[0], "grace_seconds": 300}])
    fs: dict[str, FaultState] = {}
    t0 = _lc(hass, "sensor.n_component_status")

    # First observation of not-ok starts the grace clock (streak begins at t0).
    assert _detect(hass, rules, fault_state=fs, now=t0) == []
    # Still within the grace window → not yet a fault.
    assert _detect(hass, rules, fault_state=fs, now=t0 + timedelta(seconds=200)) == []
    # Not-ok continuously past the window → flagged.
    issues = _detect(hass, rules, fault_state=fs, now=t0 + timedelta(seconds=400))
    assert len(issues) == 1


async def test_grace_survives_a_changing_bad_value(hass: HomeAssistant) -> None:
    """The grace clock is the observed not-ok STREAK, not last_changed — a value
    that keeps changing while staying not-ok still accumulates grace."""
    _device_with_sensor(
        hass, platform="esphome", object_id="n_warning_components", state="comp_a"
    )
    rules = parse_watch_rules(
        [
            {
                "name": "warn",
                "integration": "esphome",
                "match": {"entity_id_suffix": "_warning_components"},
                "ok_states": ["none", ""],
                "grace_seconds": 300,
            }
        ]
    )
    fs: dict[str, FaultState] = {}
    t0 = _lc(hass, "sensor.n_warning_components")

    assert _detect(hass, rules, fault_state=fs, now=t0) == []
    # The offending component changes (a new last_changed) but it's still not-ok.
    hass.states.async_set("sensor.n_warning_components", "comp_a,comp_b")
    assert _detect(hass, rules, fault_state=fs, now=t0 + timedelta(seconds=200)) == []
    # 400 s of continuous not-ok (despite the value change) → flagged.
    issues = _detect(hass, rules, fault_state=fs, now=t0 + timedelta(seconds=400))
    assert len(issues) == 1
    assert issues[0].detail.endswith("comp_a,comp_b") or "comp" in issues[0].detail


async def test_clear_hysteresis_holds_then_clears(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="warning"
    )
    rules = parse_watch_rules(
        [{**ESPHOME_RULE[0], "grace_seconds": 0, "clear_seconds": 300}]
    )
    fs: dict[str, FaultState] = {}
    bad_lc = _lc(hass, "sensor.n_component_status")

    # grace 0 → flags on first observation; since captures the not-ok transition.
    issues = _detect(hass, rules, fault_state=fs, now=bad_lc)
    assert len(issues) == 1
    assert issues[0].since == bad_lc

    # Recovers to ok; the clear-hold clock starts at this first ok observation.
    hass.states.async_set("sensor.n_component_status", "ok")
    t_ok = bad_lc + timedelta(seconds=10)
    held = _detect(hass, rules, fault_state=fs, now=t_ok)
    assert len(held) == 1
    assert held[0].since == bad_lc  # duration keeps the original not-ok start

    # Ok, but still within clear_seconds → still held.
    assert (
        len(_detect(hass, rules, fault_state=fs, now=t_ok + timedelta(seconds=200)))
        == 1
    )
    # Ok long enough → cleared.
    assert _detect(hass, rules, fault_state=fs, now=t_ok + timedelta(seconds=400)) == []
    assert fs == {}


async def test_flap_back_to_bad_during_hold_keeps_since(hass: HomeAssistant) -> None:
    """Returning to not-ok within the clear-hold resumes the SAME episode: one
    fault, original since preserved, no fresh grace wait."""
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="warning"
    )
    rules = parse_watch_rules(
        [{**ESPHOME_RULE[0], "grace_seconds": 0, "clear_seconds": 600}]
    )
    fs: dict[str, FaultState] = {}
    bad_lc = _lc(hass, "sensor.n_component_status")

    first = _detect(hass, rules, fault_state=fs, now=bad_lc)
    assert len(first) == 1
    since0 = first[0].since

    # Recover → held.
    hass.states.async_set("sensor.n_component_status", "ok")
    t_ok = bad_lc + timedelta(seconds=60)
    assert len(_detect(hass, rules, fault_state=fs, now=t_ok)) == 1

    # Back to not-ok within the hold.
    hass.states.async_set("sensor.n_component_status", "error")
    issues = _detect(hass, rules, fault_state=fs, now=t_ok + timedelta(seconds=30))
    assert len(issues) == 1
    assert issues[0].since == since0
    assert issues[0].since == bad_lc


# --- binary trigger + sibling text detail ------------------------------------


def _esphome_binary_plus_text(
    hass: HomeAssistant, object_id: str, *, binary: str, text: str
) -> tuple[str, str]:
    entry = MockConfigEntry(domain="esphome", title="esphome")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("esphome", object_id)}
    )
    b = ent_reg.async_get_or_create(
        "binary_sensor",
        "esphome",
        f"{object_id}_component_warning_uid",
        suggested_object_id=f"{object_id}_component_warning",
        device_id=device.id,
    )
    txt = ent_reg.async_get_or_create(
        "sensor",
        "esphome",
        f"{object_id}_warning_components_uid",
        suggested_object_id=f"{object_id}_warning_components",
        device_id=device.id,
    )
    hass.states.async_set(b.entity_id, binary)
    hass.states.async_set(txt.entity_id, text)
    return b.entity_id, txt.entity_id


_BINARY_RULE = [
    {
        "name": "ESPHome component warnings",
        "integration": "esphome",
        "match": {"entity_id_suffix": "_component_warning"},
        "ok_states": ["off"],
        "detail_entity_suffix": "_warning_components",
        "detail": "Warning components: {detail_state}",
    }
]


async def test_binary_trigger_names_component_from_sibling(hass: HomeAssistant) -> None:
    binary_id, _ = _esphome_binary_plus_text(
        hass, "hot_water", binary="on", text="dallas_temp.sensor"
    )
    issues = _detect(hass, parse_watch_rules(_BINARY_RULE))
    assert len(issues) == 1
    assert issues[0].entity_id == binary_id  # triggered on the binary sensor
    assert issues[0].detail == "Warning components: dallas_temp.sensor"


async def test_binary_ok_does_not_flag(hass: HomeAssistant) -> None:
    _esphome_binary_plus_text(hass, "hot_water", binary="off", text="None")
    assert _detect(hass, parse_watch_rules(_BINARY_RULE)) == []


async def test_binary_flags_even_when_sibling_missing(hass: HomeAssistant) -> None:
    """If the text sensor failed to populate, the device is still flagged (without
    the component name)."""
    entry = MockConfigEntry(domain="esphome", title="esphome")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("esphome", "no_text")}
    )
    b = ent_reg.async_get_or_create(
        "binary_sensor",
        "esphome",
        "no_text_component_warning_uid",
        suggested_object_id="no_text_component_warning",
        device_id=device.id,
    )
    hass.states.async_set(b.entity_id, "on")  # no sibling text sensor exists
    issues = _detect(hass, parse_watch_rules(_BINARY_RULE))
    assert len(issues) == 1
    assert issues[0].detail == "Warning components: "  # empty {detail_state}


async def test_rules_removed_clears_held_faults(hass: HomeAssistant) -> None:
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="warning"
    )
    rules = parse_watch_rules([{**ESPHOME_RULE[0], "clear_seconds": 600}])
    fs: dict[str, FaultState] = {}
    assert len(_detect(hass, rules, fault_state=fs)) == 1 and fs
    # Rules gone (file deleted) → held state dropped, no faults.
    assert (
        detect_watch_issues(
            hass, _tuples(hass), rules=[], fault_state=fs, now=dt_util.utcnow()
        )
        == []
    )
    assert fs == {}


# --- offline freezes (does NOT clear) + restart survival ---------------------


async def test_offline_freezes_fault_not_clears(hass: HomeAssistant) -> None:
    """A faulting device that goes unavailable is HELD (frozen), never cleared by
    the clear-hold, until a real value returns."""
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="warning"
    )
    rules = parse_watch_rules(
        [{**ESPHOME_RULE[0], "grace_seconds": 0, "clear_seconds": 300}]
    )
    fs: dict[str, FaultState] = {}
    bad_lc = _lc(hass, "sensor.n_component_status")
    assert len(_detect(hass, rules, fault_state=fs, now=bad_lc)) == 1  # active

    # Device drops offline (ignore_unavailable default) → frozen, still flagged …
    hass.states.async_set("sensor.n_component_status", "unavailable")
    t1 = bad_lc + timedelta(seconds=10)
    assert len(_detect(hass, rules, fault_state=fs, now=t1)) == 1
    # … and STILL flagged far beyond clear_seconds of being offline (not cleared).
    assert (
        len(_detect(hass, rules, fault_state=fs, now=t1 + timedelta(seconds=5000))) == 1
    )

    # A genuine ok return then starts the clear-hold, which eventually clears.
    hass.states.async_set("sensor.n_component_status", "ok")
    t2 = t1 + timedelta(seconds=5000)
    assert len(_detect(hass, rules, fault_state=fs, now=t2)) == 1  # held
    assert _detect(hass, rules, fault_state=fs, now=t2 + timedelta(seconds=400)) == []


async def test_pending_grace_not_reset_by_offline_blip(hass: HomeAssistant) -> None:
    """A fault still within its trigger grace that briefly goes offline must not
    have its grace clock restarted."""
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="warning"
    )
    rules = parse_watch_rules([{**ESPHOME_RULE[0], "grace_seconds": 300}])
    fs: dict[str, FaultState] = {}
    t0 = _lc(hass, "sensor.n_component_status")

    # Not-ok observed → pending, grace streak starts at t0.
    assert _detect(hass, rules, fault_state=fs, now=t0) == []
    # Briefly offline (frozen) mid-grace — must NOT restart the grace streak.
    hass.states.async_set("sensor.n_component_status", "unavailable")
    assert _detect(hass, rules, fault_state=fs, now=t0 + timedelta(seconds=100)) == []
    assert _detect(hass, rules, fault_state=fs, now=t0 + timedelta(seconds=200)) == []
    # Back to not-ok; total continuous not-ok since t0 now exceeds the grace → flag.
    hass.states.async_set("sensor.n_component_status", "warning")
    issues = _detect(hass, rules, fault_state=fs, now=t0 + timedelta(seconds=400))
    assert len(issues) == 1


async def test_ongoing_fault_flags_immediately_after_restart(
    hass: HomeAssistant,
) -> None:
    """A fresh fault_state (as after a restart) must not re-impose the trigger grace
    on a fault ongoing since before Vigil observed it — grace seeds from the
    entity's own last_changed."""
    _device_with_sensor(
        hass, platform="esphome", object_id="n_component_status", state="warning"
    )
    rules = parse_watch_rules([{**ESPHOME_RULE[0], "grace_seconds": 300}])
    lc = _lc(hass, "sensor.n_component_status")
    # First observation is an hour after the entity went bad → flags immediately.
    issues = _detect(hass, rules, fault_state={}, now=lc + timedelta(hours=1))
    assert len(issues) == 1


async def test_persisted_fault_kept_when_entity_not_yet_loaded(
    hass: HomeAssistant,
) -> None:
    """A restored fault whose entity is registered but has no state yet (its
    integration is mid-reconnect on the first post-restart cycle) must be kept —
    not purged as "vanished" and re-seeded fresh, which would reset its since."""
    entry = MockConfigEntry(domain="esphome", title="esphome")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("esphome", "n")}
    )
    ent = ent_reg.async_get_or_create(
        "binary_sensor",
        "esphome",
        "n_cw_uid",
        suggested_object_id="node_component_warning",
        device_id=device.id,
    )
    # No hass.states.async_set → the entity is registered but has NO state yet.
    key = ent.entity_id
    since = dt_util.utcnow() - timedelta(hours=5)
    fs = {
        key: FaultState(
            phase=FaultPhase.ACTIVE,
            streak_since=since,
            since=since,
            detail="Failed: x",
            source="r",
            domain="esphome",
            clear_seconds=0,
        )
    }
    rules = parse_watch_rules(
        [
            {
                "name": "r",
                "integration": "esphome",
                "match": {"entity_id_suffix": "_component_warning"},
                "ok_states": ["off"],
            }
        ]
    )
    _detect(hass, rules, fault_state=fs)

    assert key in fs, "restored fault was purged while its entity was mid-reconnect"
    assert fs[key].since == since  # original start preserved


async def test_fault_purged_when_entity_deregistered(hass: HomeAssistant) -> None:
    """A tracked fault whose entity is gone from the registry (no state, not
    registered) IS purged — the keep-frozen behavior is only for registered ones."""
    now = dt_util.utcnow()
    key = "binary_sensor.removed_component_warning"
    fs = {
        key: FaultState(
            phase=FaultPhase.ACTIVE,
            streak_since=now,
            since=now,
            detail="x",
            source="r",
            domain="esphome",
            clear_seconds=0,
        )
    }
    rules = parse_watch_rules(
        [
            {
                "name": "r",
                "integration": "esphome",
                "match": {"entity_id_suffix": "_component_warning"},
                "ok_states": ["off"],
            }
        ]
    )
    _detect(hass, rules, fault_state=fs)
    assert key not in fs


# --- persistence roundtrip ---------------------------------------------------


def test_fault_state_serialize_roundtrip() -> None:
    from custom_components.vigil.detection.engines.watch_config import (
        deserialize_fault_state,
        serialize_fault_state,
    )

    t = dt_util.utcnow()
    fs = {
        "sensor.x": FaultState(
            phase=FaultPhase.ACTIVE,
            streak_since=t,
            since=t,
            detail="Failed: a",
            source="r",
            domain="esphome",
            clear_seconds=900,
        ),
        "sensor.y": FaultState(
            phase=FaultPhase.PENDING,  # transient → must NOT be persisted
            streak_since=t,
            since=t,
            detail="",
            source="r",
            domain="esphome",
            clear_seconds=0,
        ),
    }
    data = serialize_fault_state(fs)
    assert set(data) == {"sensor.x"}  # pending dropped
    back = deserialize_fault_state(data)
    assert back["sensor.x"].phase == "active"
    assert back["sensor.x"].detail == "Failed: a"
    assert back["sensor.x"].clear_seconds == 900
    assert abs((back["sensor.x"].since - t).total_seconds()) < 1


def test_deserialize_fault_state_tolerates_garbage() -> None:
    from custom_components.vigil.detection.engines.watch_config import (
        deserialize_fault_state,
    )

    assert deserialize_fault_state(None) == {}
    assert deserialize_fault_state("not a dict") == {}
    # Missing timestamps / bad phase are skipped, not fatal.
    assert deserialize_fault_state({"sensor.x": {"phase": "active"}}) == {}
    assert deserialize_fault_state({"sensor.x": {"phase": "pending"}}) == {}


# --- fault onset survives a mid-reload tuple gap -----------------------------


async def test_fault_kept_when_device_absent_from_tuples(hass: HomeAssistant) -> None:
    """A persisted/active fault must survive a cycle where its entity is present in
    ``hass.states`` but its device isn't in this cycle's tuples (e.g. the device is
    mid-reload right after a restart). Dropping it there would re-create it next
    cycle from a reset ``last_changed``, resetting the fault's ``since``."""
    _device_with_sensor(
        hass, platform="esphome", object_id="node_component_status", state="warning"
    )
    ent_id = "sensor.node_component_status"
    t0 = dt_util.utcnow() - timedelta(hours=14)
    fault_state = {
        ent_id: FaultState(
            phase=FaultPhase.ACTIVE,
            streak_since=t0,
            since=t0,
            detail="x",
            source="r",
            domain="esphome",
            clear_seconds=900,
        )
    }
    rules = parse_watch_rules(ESPHOME_RULE)

    # Cycle with the device NOT in this cycle's tuples (mid-reload). The entity is
    # still registered and still has a state — it just wasn't evaluated.
    detect_watch_issues(
        hass, [], rules=rules, fault_state=fault_state, now=dt_util.utcnow()
    )
    assert ent_id in fault_state, "fault dropped while its device was mid-reload"
    assert fault_state[ent_id].since == t0  # onset preserved

    # Its state's last_changed just reset (the unavailable->warning reconnect blip);
    # because the fault survived, the re-observed not-ok keeps the original since.
    hass.states.async_set(ent_id, "warning")
    issues = detect_watch_issues(
        hass, _tuples(hass), rules=rules, fault_state=fault_state, now=dt_util.utcnow()
    )
    assert len(issues) == 1
    assert issues[0].since == t0, "onset must survive the reload, not reset to the blip"


async def test_fault_dropped_when_evaluated_but_rule_no_longer_matches(
    hass: HomeAssistant,
) -> None:
    """The intended purge still works: an entity present in this cycle's tuples that
    no longer matches any rule (e.g. the rule was removed) drops its fault."""
    _device_with_sensor(
        hass, platform="esphome", object_id="node_component_status", state="warning"
    )
    ent_id = "sensor.node_component_status"
    fault_state = {
        ent_id: FaultState(
            phase=FaultPhase.ACTIVE,
            streak_since=dt_util.utcnow(),
            since=dt_util.utcnow(),
            detail="x",
            source="r",
            domain="esphome",
            clear_seconds=0,
        )
    }
    # Rules empty → the entity is evaluated (device in tuples) but matches nothing.
    detect_watch_issues(
        hass, _tuples(hass), rules=[], fault_state=fault_state, now=dt_util.utcnow()
    )
    assert ent_id not in fault_state  # genuinely no longer watched → dropped
