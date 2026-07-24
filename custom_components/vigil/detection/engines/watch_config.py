# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Engine 4 configuration and cross-cycle state.

Owns Engine 4's non-detection halves: the :class:`WatchRule` model + schema +
:func:`parse_watch_rules`, the :class:`VigilConfigStore` that loads and caches the
deployment's ``vigil.yaml`` (in the HA config dir, not this repo; the legacy
watch-only ``vigil_watch.yaml`` is read as a fallback when it is absent), and
:class:`FaultState` persistence. The detection pass lives in
``engines/engine4_watch_rules.py``.

Example ``<config>/vigil.yaml`` (see ``vigil.example.yaml`` for the full form)::

    watch:
      - name: ESPHome component health
        integration: esphome          # the entity's platform MUST be this
        match:                        # at least one criterion, all must match
          entity_id_glob: "sensor.*_component_*"
        ok_states: ["ok", "none", ""]
        ignore_unavailable: true
        detail: "Component fault: {state}"
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import load_yaml
from pydantic import BaseModel

from ...const import STATE_FAULT_KEY, VIGIL_CONFIG_FILE, WATCH_RULES_FILE
from ...models import AwareUTC, DeviceTuple, FaultPhase
from ...selectors import EntitySelector
from ...storage import StateStore, StoreRepo, dump_model_map, load_model_map

_LOGGER = logging.getLogger(__name__)

_MATCH_KEYS = ("entity_id_glob", "entity_id_suffix", "device_class", "translation_key")


def _match_has_criterion(match: dict[str, Any]) -> dict[str, Any]:
    if not any(match.get(k) for k in _MATCH_KEYS):
        raise vol.Invalid("match must set at least one of: " + ", ".join(_MATCH_KEYS))
    return match


_MATCH_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("entity_id_glob"): str,
            vol.Optional("entity_id_suffix"): str,
            vol.Optional("device_class"): str,
            vol.Optional("translation_key"): str,
        }
    ),
    _match_has_criterion,
)

_RULE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("integration"): str,
        vol.Required("match"): _MATCH_SCHEMA,
        vol.Optional("ok_states", default=["ok"]): [vol.Coerce(str)],
        vol.Optional("ignore_unavailable", default=True): bool,
        vol.Optional("case_sensitive", default=False): bool,
        # Trigger debounce and clear hysteresis, in seconds.
        vol.Optional("grace_seconds", default=0): vol.All(int, vol.Range(min=0)),
        vol.Optional("clear_seconds", default=0): vol.All(int, vol.Range(min=0)),
        # Optionally pull ``{detail_state}`` from a sibling entity on the device.
        vol.Optional("detail_entity_suffix"): str,
        vol.Optional("detail_entity_glob"): str,
        vol.Optional("detail", default="Not OK: {state}"): str,
    }
)


@dataclass(frozen=True)
class WatchRule:
    """A parsed, normalized watch rule (see the module docstring)."""

    name: str
    selector: EntitySelector
    ok_states: frozenset[str]
    ignore_unavailable: bool
    case_sensitive: bool
    grace_seconds: int
    clear_seconds: int
    detail_entity_suffix: str | None
    detail_entity_glob: str | None
    detail: str

    @property
    def integration(self) -> str:
        """The rule's integration — always set for a watch rule; used for grouping."""
        return self.selector.integration or ""

    def entity_matches(self, entry: er.RegistryEntry, state: State) -> bool:
        """Whether this rule's selector matches the entity."""
        return self.selector.matches(entry, state)

    def is_ok(self, raw_state: str) -> bool:
        """Whether ``raw_state`` counts as healthy for this rule."""
        probe = raw_state if self.case_sensitive else raw_state.casefold()
        return probe in self.ok_states

    def detail_source(self, state: State, tuple_: DeviceTuple) -> tuple[str, str]:
        """The ``{detail_state}`` / ``{detail_entity_id}`` for the message.

        The matched entity itself by default; if ``detail_entity_suffix`` /
        ``detail_entity_glob`` is set, the first matching sibling on the device
        (or empty strings if none matches).
        """
        if self.detail_entity_suffix is None and self.detail_entity_glob is None:
            return state.state, state.entity_id
        for s in tuple_.entity_states:
            eid = s.entity_id
            if self.detail_entity_suffix is not None and not eid.endswith(
                self.detail_entity_suffix
            ):
                continue
            if self.detail_entity_glob is not None and not fnmatch.fnmatchcase(
                eid, self.detail_entity_glob
            ):
                continue
            return s.state, eid
        return "", ""

    def render_detail(self, state: State, tuple_: DeviceTuple) -> str:
        """The issue detail, with ``{state}``/``{entity_id}``/``{name}`` and the
        sibling ``{detail_state}``/``{detail_entity_id}`` filled in.

        Falls back to the raw template on any formatting error, so a bad template
        can never break the detection cycle.
        """
        detail_state, detail_entity_id = self.detail_source(state, tuple_)
        try:
            return self.detail.format(
                state=state.state,
                entity_id=state.entity_id,
                name=tuple_.device_name,
                detail_state=detail_state,
                detail_entity_id=detail_entity_id,
            )
        except Exception:  # noqa: BLE001 - a bad template must not break detection
            return self.detail


def _rule_label(entry: Any) -> str:
    """A best-effort name for an invalid rule, for the skip log."""
    if isinstance(entry, dict):
        name = entry.get("name")
        if isinstance(name, str) and name:
            return name
    return "unnamed"


def _parse_rule_list[T](
    raw: Any,
    *,
    schema: vol.Schema,
    build: Callable[[dict[str, Any]], T],
    kind: str,
    not_list_msg: str,
    with_label: bool = False,
) -> list[T]:
    """Shared validation skeleton for the watch/ignore rule parsers.

    Each entry is validated independently: a malformed entry is logged and skipped
    while the rest load. ``vol.Invalid`` is raised only when the input is wholesale
    unusable (not a list, or every entry invalid), so the caller keeps its
    last-good config on a fully-broken edit. ``build`` turns a validated entry into
    the concrete rule/selector; ``kind`` is the singular noun for log/error text.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise vol.Invalid(not_list_msg.format(got=type(raw).__name__))
    items: list[T] = []
    errors = 0
    for index, entry in enumerate(raw):
        try:
            validated: dict[str, Any] = schema(entry)
        except vol.Invalid as err:
            errors += 1
            if with_label:
                _LOGGER.error(
                    "Vigil: skipping invalid %s #%d (%s): %s",
                    kind,
                    index + 1,
                    _rule_label(entry),
                    err,
                )
            else:
                _LOGGER.error(
                    "Vigil: skipping invalid %s #%d: %s", kind, index + 1, err
                )
            continue
        items.append(build(validated))
    if not items and errors:
        # Every entry invalid → raise so the caller keeps its last-good config.
        raise vol.Invalid(f"all {errors} {kind}(s) are invalid")
    return items


def _build_watch_rule(r: dict[str, Any]) -> WatchRule:
    """Assemble a :class:`WatchRule` from a schema-validated rule mapping."""
    case_sensitive = r["case_sensitive"]
    ok_states = frozenset(s if case_sensitive else s.casefold() for s in r["ok_states"])
    return WatchRule(
        name=r["name"],
        selector=EntitySelector.from_match(r["integration"], r["match"]),
        ok_states=ok_states,
        ignore_unavailable=r["ignore_unavailable"],
        case_sensitive=case_sensitive,
        grace_seconds=r["grace_seconds"],
        clear_seconds=r["clear_seconds"],
        detail_entity_suffix=r.get("detail_entity_suffix"),
        detail_entity_glob=r.get("detail_entity_glob"),
        detail=r["detail"],
    )


def parse_watch_rules(raw: Any) -> list[WatchRule]:
    """Validate + normalize raw YAML content into :class:`WatchRule`s.

    A malformed rule is logged and skipped while the rest load; a wholesale-broken
    file (not a list, or every rule invalid) raises so the caller keeps its
    last-good rules.
    """
    return _parse_rule_list(
        raw,
        schema=_RULE_SCHEMA,
        build=_build_watch_rule,
        kind="watch rule",
        not_list_msg="watch-rules file must be a list of rules, got {got}",
        with_label=True,
    )


# Phase 1: the only ignore action is "treat this entity as NOT a connectivity
# signal" (a mislabeled device_class=connectivity sensor). More actions later.
_IGNORE_ACTIONS = ("connectivity",)

_IGNORE_SCHEMA = vol.Schema(
    {
        vol.Required("action"): vol.In(_IGNORE_ACTIONS),
        vol.Optional("integration"): str,
        vol.Required("match"): _MATCH_SCHEMA,
    }
)


def parse_ignore_rules(raw: Any) -> list[EntitySelector]:
    """Validate the ``ignore:`` section into connectivity-ignore selectors.

    Each entry pairs an ``action`` (only ``connectivity`` today) with an optional
    integration and a ``match`` selector. A malformed entry is logged and skipped;
    a wholesale-invalid section raises so the store keeps its last-good config
    rather than silently dropping every rule (which would trust a mislabeled
    connectivity sensor again — a wrong detection).
    """
    return _parse_rule_list(
        raw,
        schema=_IGNORE_SCHEMA,
        build=lambda e: EntitySelector.from_match(e.get("integration"), e["match"]),
        kind="ignore rule",
        not_list_msg="'ignore' must be a list of rules, got {got}",
    )


@dataclass(frozen=True)
class VigilFileConfig:
    """The parsed contents of ``vigil.yaml``."""

    watch_rules: list[WatchRule]
    ignore_connectivity: list[EntitySelector]


def parse_vigil_config(raw: Any) -> VigilFileConfig:
    """Parse ``vigil.yaml`` — a mapping with optional ``watch`` / ``ignore`` sections.

    A top-level LIST is accepted as a watch-only file (the legacy
    ``vigil_watch.yaml`` format), so an unmigrated file still loads its rules.
    """
    if isinstance(raw, list):  # legacy vigil_watch.yaml: a flat list of watch rules
        return VigilFileConfig(parse_watch_rules(raw), [])
    if raw is None:
        return VigilFileConfig([], [])
    if not isinstance(raw, dict):
        raise vol.Invalid(
            "vigil.yaml must be a mapping with 'watch'/'ignore' sections, got "
            f"{type(raw).__name__}"
        )
    return VigilFileConfig(
        watch_rules=parse_watch_rules(raw.get("watch")),
        ignore_connectivity=parse_ignore_rules(raw.get("ignore")),
    )


def _signature(path: str) -> tuple[float, int] | None:
    """(mtime, size) for change detection — ``None`` if the file is absent.

    Keying on size too catches an edit that preserves the timestamp.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


class VigilConfigStore:
    """Loads + caches the deployment's ``vigil.yaml`` (watch + ignore rules),
    re-reading only when the file changes. Absent = no rules; a parse error keeps
    the last-good config. Falls back to the legacy ``vigil_watch.yaml``
    (watch-only) when ``vigil.yaml`` is absent."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # vigil.yaml wins; the legacy watch-only file is the fallback.
        self._paths = (
            hass.config.path(VIGIL_CONFIG_FILE),
            hass.config.path(WATCH_RULES_FILE),
        )
        self._key: tuple[str, tuple[float, int]] | None = None
        self._config = VigilFileConfig([], [])
        self._seen = False

    async def async_get_watch_rules(self) -> list[WatchRule]:
        """The current watch rules, reloading if the config file changed."""
        await self._maybe_reload()
        return self._config.watch_rules

    async def async_get_ignore_connectivity(self) -> list[EntitySelector]:
        """The current connectivity-ignore selectors, reloading if the file changed."""
        await self._maybe_reload()
        return self._config.ignore_connectivity

    async def _maybe_reload(self) -> None:
        """(Re)parse the effective config file when it (or the choice of it) changes."""
        chosen: str | None = None
        sig: tuple[float, int] | None = None
        for path in self._paths:
            s = await self._hass.async_add_executor_job(_signature, path)
            if s is not None:
                chosen, sig = path, s
                break
        if chosen is None or sig is None:
            if self._seen and (
                self._config.watch_rules or self._config.ignore_connectivity
            ):
                _LOGGER.info("Vigil: config file removed; watch/ignore rules disabled")
                self._config = VigilFileConfig([], [])
            self._key = None
            self._seen = True
            return
        key = (chosen, sig)
        if self._seen and key == self._key:
            return
        try:
            raw = await self._hass.async_add_executor_job(load_yaml, chosen)
            self._config = parse_vigil_config(raw)
            _LOGGER.info(
                "Vigil: loaded %d watch + %d ignore rule(s) from %s",
                len(self._config.watch_rules),
                len(self._config.ignore_connectivity),
                chosen,
            )
        except Exception as err:  # noqa: BLE001 - a bad file must never break setup/cycle
            _LOGGER.error(
                "Vigil: config file %s is invalid; keeping the previous config: %s",
                chosen,
                err,
            )
        # Advance the key regardless so a broken file isn't re-parsed every cycle;
        # the next save triggers a retry.
        self._key = key
        self._seen = True


class FaultState(BaseModel):
    """Cross-cycle state for one tracked watch entity (keyed by entity id).

    The debounce clocks run off ``streak_since`` (the observed start of the
    current ok/not-ok category streak), not ``last_changed``, so a value change
    within a streak doesn't re-arm them.

    * ``phase`` — ``pending`` (awaiting trigger grace), ``active`` (flagged), or
      ``holding`` (ok again, awaiting clear hysteresis).
    * ``streak_since`` — when the current category streak was first observed.
    * ``since`` — the ``last_changed`` at first not-ok, kept for display.
    * ``detail``/``source``/``domain`` — captured so the fault renders during the
      clear-hold.
    * ``clear_seconds`` — the governing rule's hysteresis, remembered for the hold.
    """

    phase: FaultPhase
    streak_since: AwareUTC
    since: AwareUTC
    detail: str = ""
    source: str = ""
    domain: str = ""
    clear_seconds: int = 0


def _is_persisted_phase(fs: FaultState) -> bool:
    """``pending`` is transient and never persisted — only active/holding faults."""
    return fs.phase in (FaultPhase.ACTIVE, FaultPhase.HOLDING)


def serialize_fault_state(fault_state: dict[str, FaultState]) -> dict[str, Any]:
    """JSON-serializable form for persistence. Only ``active``/``holding`` faults
    are kept; ``pending`` is transient and re-derived next cycle."""
    return dump_model_map(
        {key: fs for key, fs in fault_state.items() if _is_persisted_phase(fs)}
    )


def deserialize_fault_state(data: Any) -> dict[str, FaultState]:
    """Rebuild the fault-state map from persisted JSON, skipping malformed rows
    (and any legacy ``pending`` row, which is transient and never persisted)."""
    return load_model_map(FaultState, data, keep=_is_persisted_phase)


class RuleFaultRepo(StoreRepo[dict[str, FaultState]]):
    """Persisted per-rule fault/debounce state (Engine 4).

    Owns the in-memory ``dict[str, FaultState]`` so the engine gets a live map to
    mutate. Only ``active``/``holding`` faults are written, so an in-progress fault
    and its clear-hold survive a restart/reload.
    """

    def __init__(self, hass: HomeAssistant, store: StateStore) -> None:
        super().__init__(
            hass,
            store,
            key=STATE_FAULT_KEY,
            initial={},
            serialize=serialize_fault_state,
            deserialize=deserialize_fault_state,
        )
