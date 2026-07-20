# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Engine 4 — declarative watch rules (detection pass).

Flag a device when one of its entities holds a non-"ok" value per the
deployment's rules. Each rule matches on an integration (the entity's platform)
AND an entity criterion; a matched entity whose state is not in ``ok_states``
produces a ``DEVICE_FAULT`` :class:`VigilIssue` attributed to its device.

The rule model, rules-file store, and fault-state model live in ``watch_config.py``.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from ...const import NO_VALUE_STATES
from ...models import DeviceTuple, FaultPhase, IssueKind, VigilIssue
from .watch_config import FaultState, WatchRule


def _classify(
    candidates: list[WatchRule], entry: er.RegistryEntry, state: State
) -> tuple[str, WatchRule | None]:
    """Classify a matched entity this cycle:

    * ``("bad", rule)`` — flagged by the first matching rule that flags.
    * ``("frozen", None)`` — unavailable/unknown and every matching rule ignores it.
    * ``("ok", None)`` — matches a rule and reads a healthy value.
    * ``("nomatch", None)`` — no rule matches this entity at all.
    """
    matched = False
    ignored_unavailable = False
    for rule in candidates:
        if not rule.entity_matches(entry, state):
            continue
        matched = True
        if rule.ignore_unavailable and state.state in NO_VALUE_STATES:
            ignored_unavailable = True
            continue
        if rule.is_ok(state.state):
            continue
        return "bad", rule
    if not matched:
        return "nomatch", None
    if ignored_unavailable and state.state in NO_VALUE_STATES:
        return "frozen", None
    return "ok", None


def detect_watch_issues(
    hass: HomeAssistant,
    tuples: list[DeviceTuple],
    *,
    rules: list[WatchRule],
    fault_state: dict[str, FaultState],
    now: datetime,
) -> list[VigilIssue]:
    """Evaluate the watch rules against the current device snapshot (Engine 4).

    One issue per faulted entity, attributed to its device. Two per-rule debounces
    smooth flapping, timed from the observed streak (see :class:`FaultState`):
    ``grace_seconds`` (must be not-ok this long before flagging) and
    ``clear_seconds`` (once flagged, held until ok this long). ``fault_state`` is
    the coordinator's cross-cycle map, mutated in place.
    """
    if not rules:
        # Feature turned off (file removed) — drop any tracked faults.
        fault_state.clear()
        return []
    rules_by_integration: dict[str, list[WatchRule]] = {}
    for rule in rules:
        rules_by_integration.setdefault(rule.integration, []).append(rule)

    ent_reg = er.async_get(hass)
    issues: list[VigilIssue] = []
    seen: set[str] = set()
    for t in tuples:
        for state in t.entity_states:
            entry = ent_reg.async_get(state.entity_id)
            if entry is None:
                continue
            candidates = rules_by_integration.get(entry.platform)
            if not candidates:
                continue
            category, flag_rule = _classify(candidates, entry, state)
            if category == "nomatch":
                continue

            key = state.entity_id
            seen.add(key)
            prev = fault_state.get(key)

            if category == "frozen":
                # Unavailable/offline is NOT recovery. For a shown (active/holding)
                # fault, freeze the clocks (reset the streak to ``now``) and keep
                # it flagged.
                if prev is not None:
                    if prev.phase in (FaultPhase.ACTIVE, FaultPhase.HOLDING):
                        prev.streak_since = now
                        issues.append(_fault_issue(t, entry, key, prev))
                    else:
                        # A never-confirmed pending fault whose entity went
                        # unavailable is now an offline concern (Engine 2's job),
                        # not a fault: abandon it rather than hold a debounce that
                        # can never flag (and would otherwise leak indefinitely if
                        # the entity stays unavailable).
                        del fault_state[key]
            elif flag_rule is not None:
                # NOT-OK this cycle.
                if prev is None:
                    # Seed the grace clock from the entity's own transition (capped
                    # at ``now``), not ``now``, so an already-ongoing fault isn't
                    # re-suppressed for a fresh grace after a reload/restart.
                    prev = FaultState(
                        phase=FaultPhase.PENDING,
                        streak_since=min(now, state.last_changed),
                        since=state.last_changed,
                        detail="",
                        source=flag_rule.name,
                        domain=entry.platform,
                        clear_seconds=flag_rule.clear_seconds,
                    )
                    fault_state[key] = prev
                elif prev.phase == FaultPhase.HOLDING:
                    # Was recovering, not-ok again → resume the same active episode.
                    prev.phase = FaultPhase.ACTIVE
                # Refresh the live bits (the offending value may have changed).
                prev.detail = flag_rule.render_detail(state, t)
                prev.source = flag_rule.name
                prev.domain = entry.platform
                prev.clear_seconds = flag_rule.clear_seconds
                if (
                    prev.phase == FaultPhase.PENDING
                    and (now - prev.streak_since).total_seconds()
                    >= flag_rule.grace_seconds
                ):
                    prev.phase = FaultPhase.ACTIVE  # trigger grace satisfied
                if prev.phase == FaultPhase.ACTIVE:
                    issues.append(_fault_issue(t, entry, key, prev))
                # else still pending → no issue this cycle
            elif prev is not None:
                # OK / ignored this cycle, but we were tracking it.
                if prev.phase == FaultPhase.PENDING:
                    # Never flagged (recovered before grace elapsed) → abandon.
                    del fault_state[key]
                    continue
                if prev.phase == FaultPhase.ACTIVE:
                    # First ok observation → start the clear-hold streak.
                    prev.phase = FaultPhase.HOLDING
                    prev.streak_since = now
                if (now - prev.streak_since).total_seconds() >= prev.clear_seconds:
                    del fault_state[key]  # ok long enough → cleared
                else:
                    issues.append(_fault_issue(t, entry, key, prev))  # held

    # Drop a tracked fault only when it is genuinely gone: the entity WAS evaluated
    # this cycle (its device was in ``tuples``) but no longer matches any rule, OR
    # it has left the registry. An entity that isn't in this cycle's tuples at all
    # — its device/integration is mid-reload, e.g. right after a restart — is kept
    # frozen, so a persisted fault isn't purged and re-seeded fresh (which would
    # reset its ``since`` to the reconnect blip). Presence in ``hass.states`` is
    # NOT proof it was evaluated: a device absent from ``tuples`` can still have a
    # lingering state.
    evaluated = {s.entity_id for t in tuples for s in t.entity_states}
    for key in list(fault_state):
        if key in seen:
            continue
        if key in evaluated or ent_reg.async_get(key) is None:
            del fault_state[key]
    return issues


def _fault_issue(
    t: DeviceTuple, entry: er.RegistryEntry, entity_id: str, fs: FaultState
) -> VigilIssue:
    return VigilIssue(
        kind=IssueKind.DEVICE_FAULT,
        name=t.device_name,
        integration=t.integration_label,
        detail=fs.detail,
        since=fs.since,
        source=fs.source,
        device_id=t.device_id,
        entity_id=entity_id,
        config_entry_id=t.config_entry_id,
        # The entity's platform — the stable key exclusions and naming use.
        domain=fs.domain,
    )
