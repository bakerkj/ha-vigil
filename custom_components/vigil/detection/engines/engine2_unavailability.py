# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from ...const import RESTART_ARTIFACT_WINDOW_SECONDS, STATE_DOWNTIME_KEY
from ...history.recorder import async_recorder_last_good
from ...models import (
    ConnectivityState,
    DeviceTuple,
    DowntimeRecord,
    IssueKind,
    VigilIssue,
)
from ...storage import StateStore, StoreRepo, dump_model_map, load_model_map

# Friendly labels for the connectivity sources used in the offline "detail".
_SOURCE_LABELS = {
    "connectivity_binary_sensor": "a connectivity sensor",
    "zwave_node_status": "Z-Wave (node reported dead)",
}


def serialize_downtime(downtime: dict[str, DowntimeRecord]) -> dict[str, Any]:
    """JSON-serializable form of the per-device downtime map."""
    return dump_model_map(downtime)


def deserialize_downtime(data: Any) -> dict[str, DowntimeRecord]:
    """Rebuild the downtime map from persisted JSON, skipping malformed rows.

    ``recorder_resolved`` round-trips, so a device whose true outage start was
    already reconstructed keeps it across a restart and the seed does NOT re-query
    the recorder for it — trusting the persisted precise ``since`` is also more
    accurate than re-querying, which can only re-floor a long outage to the (now
    shorter) recorder window. Legacy rows with no flag load as unresolved, so they
    self-heal with one re-query on the upgrade restart.
    """
    return load_model_map(DowntimeRecord, data)


class DowntimeRepo(StoreRepo[dict[str, DowntimeRecord]]):
    """Persisted per-device offline records (Engine 2).

    The recorder is the primary source of the true outage start; this repo is the
    fallback for recorder-blind devices, whose downtime would otherwise reset on a
    restart. Owns the live map the coordinator seeds and Engine 2 mutates.
    """

    def __init__(self, hass: HomeAssistant, store: StateStore) -> None:
        super().__init__(
            hass,
            store,
            key=STATE_DOWNTIME_KEY,
            initial={},
            serialize=serialize_downtime,
            deserialize=deserialize_downtime,
        )


def _offline_detail(kind: IssueKind, source: str) -> str:
    """Describe HOW Vigil concluded a device is offline."""
    if kind is IssueKind.DEVICE_OFFLINE_CONFIRMED:
        if source in _SOURCE_LABELS:
            label = _SOURCE_LABELS[source]
        elif source.startswith("mac:"):
            label = f"the router/AP ({source[len('mac:') :]})"
        else:
            label = source.replace("_", " ")
        return f"Reported down by {label}"
    if source == "zwave_node_status_asleep":
        return "Z-Wave node asleep — all entities silent"
    return "No connectivity signal — inferred from silence"


def is_offline(t: DeviceTuple) -> bool:
    """Whether a device counts as offline this cycle (no grace applied).

    Either all data entities are unavailable, or a connectivity signal reports
    DOWN with some entities unavailable (or no data entities at all).
    """
    return t.all_unavailable or (
        t.connectivity_state == ConnectivityState.DOWN
        and (t.any_unavailable or not t.data_entity_ids)
    )


def detect_unavailability_issues(
    tuples: list[DeviceTuple],
    *,
    flagged_entry_ids: set[str],
    grace_period: timedelta,
    battery_multiplier: float,
    downtime: dict[str, DowntimeRecord],
    now: datetime,
    boot_time: datetime | None = None,
    known_device_ids: set[str] | None = None,
) -> list[VigilIssue]:
    """Engine 2 — flag devices whose entities are all unavailable past grace.

    ``downtime`` is mutated in place: one :class:`DowntimeRecord` per offline
    device tracks the outage start. The coordinator pre-seeds records from the
    recorder; here we seed a fallback for newly-offline devices and drop records
    once a device recovers or is covered by Engine 1. ``boot_time`` enables a
    per-device boot grace (see below).
    """
    issues: list[VigilIssue] = []
    for t in tuples:
        # Already covered by Engine 1 — don't double-count; forget its downtime.
        if t.config_entry_id is not None and t.config_entry_id in flagged_entry_ids:
            downtime.pop(t.device_id, None)
            continue

        if is_offline(t):
            # Seed from the real transition time so an already-offline device is
            # reported immediately, not after a fresh grace from first observation.
            record = downtime.setdefault(
                t.device_id, DowntimeRecord(since=t.offline_since or now)
            )

            if t.is_battery or t.connectivity_state == ConnectivityState.UNKNOWN:
                effective_grace = grace_period * battery_multiplier
            else:
                effective_grace = grace_period

            # Per-device boot grace: a record whose evidence predates the restart
            # measures grace from boot, not the stale pre-restart timestamp — else
            # a device slow to republish is flagged when the startup grace lifts.
            # Exception: a recorder-resolved record proving the device was already
            # offline before boot fires as-is.
            effective_since = record.since
            if boot_time is not None and record.since <= boot_time:
                proven_dead_before_boot = (
                    record.recorder_resolved
                    and (boot_time - record.since) > effective_grace
                )
                if not proven_dead_before_boot:
                    effective_since = boot_time

            if (now - effective_since) > effective_grace:
                if t.connectivity_state == ConnectivityState.DOWN:
                    kind = IssueKind.DEVICE_OFFLINE_CONFIRMED
                else:
                    kind = IssueKind.DEVICE_OFFLINE_NO_SIGNAL
                issues.append(
                    VigilIssue(
                        kind=kind,
                        name=t.device_name,
                        integration=t.integration_label,
                        detail=_offline_detail(kind, t.connectivity_source),
                        source=t.connectivity_source,
                        since=effective_since,
                        device_id=t.device_id,
                        config_entry_id=t.config_entry_id,
                        domain=t.config_entry_domain,
                        since_is_lower_bound=record.is_lower_bound,
                    )
                )
        else:
            # Recovered or healthy — forget any tracked downtime.
            downtime.pop(t.device_id, None)

    # GC tracking for devices that are genuinely GONE, not merely absent from this
    # cycle's tuples: a device mid-reconnect after a restart is dropped from
    # ``tuples`` for a cycle but still in the registry, and GC'ing it there would
    # discard a persisted, non-re-derivable outage start. Prefer the registry id
    # set when supplied; else fall back to tuple presence.
    known_ids = (
        known_device_ids
        if known_device_ids is not None
        else {t.device_id for t in tuples}
    )
    for stale_id in set(downtime) - known_ids:
        downtime.pop(stale_id, None)

    return issues


async def async_seed_downtime(
    hass: HomeAssistant,
    tuples: list[DeviceTuple],
    downtime: dict[str, DowntimeRecord],
    *,
    boot_time: datetime | None,
    ha_started: bool,
    ha_started_at: datetime | None,
    lookback: timedelta,
    now: datetime,
    observed_up: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Seed ``downtime`` records from the recorder for devices offline ACROSS a
    restart, BEFORE Engine 2 reads them, so a device already down before the restart
    isn't given a fresh grace.

    Recorder reconstruction is ONLY for a device whose outage could predate our
    observation. ``observed_up`` is the set of devices seen UP this session — one
    that later drops was watched live, so its ``since`` is already known and it must
    NOT be re-reconstructed (that would be a full recorder scan per flap, all day).
    Gating additionally on ``recorder_resolved`` (not mere presence) lets later
    cycles correct fallback-seeded records rather than locking a wrong boot time in
    forever. No-op without a recorder.
    """
    if "recorder" not in hass.config.components:
        return

    pending = [
        t
        for t in tuples
        if is_offline(t)
        and t.device_id not in observed_up
        and not ((rec := downtime.get(t.device_id)) and rec.recorder_resolved)
    ]
    if not pending:
        return

    # Query exactly the entities the availability judgement is based on
    # (data_entity_ids drops signal/annotation/no-state entities). The raw
    # entity_states would pull in an annotation entity that stays available on a
    # dead device, resetting the reconstructed outage start.
    pending_by_id = {t.device_id: t for t in pending}
    entity_to_device: dict[str, str] = {}
    for t in pending:
        for entity_id in t.data_entity_ids:
            entity_to_device[entity_id] = t.device_id

    if entity_to_device:
        last_good, floored, blind, run_history_ok = await async_recorder_last_good(
            hass, entity_to_device, boot_time, now, lookback
        )
    else:
        # Only signal-only devices are pending — nothing to query; each is seeded
        # from its own signal transition below.
        last_good, floored, blind, run_history_ok = {}, set(), set(), True
    for did, t in pending_by_id.items():
        recorder_value = last_good.get(did)
        if recorder_value is not None:
            downtime[did] = DowntimeRecord(
                since=recorder_value,
                is_lower_bound=did in floored,
                # A failed run-history read may have trusted a stale restore, so
                # leave it unresolved to re-query next cycle.
                recorder_resolved=run_history_ok,
            )
        elif did in blind:
            # Recorder is BLIND to this device (zero rows for every data entity).
            # Trust a record restored from a prior session (mark it resolved so the
            # boot grace keeps its persisted ``since``); otherwise leave it unseeded
            # — Engine 2 uses live offline_since + grace — and unresolved so a later
            # cycle re-queries once rows appear. But only resolve when the read
            # actually SUCCEEDED: on a total history-read failure every device is
            # reported blind, and marking a persisted record resolved then would
            # lock a stale ``since`` and never re-query, defeating self-correction.
            rec = downtime.get(did)
            if rec is not None and run_history_ok:
                rec.recorder_resolved = True
            continue
        else:
            # A signal-only device reporting DOWN: seed from its connectivity
            # signal's ``last_changed`` so grace absorbs a blip and a genuine outage
            # reports its true duration. Leave unseeded if no signal.
            signal_since = max(
                (
                    s.last_changed
                    for s in t.entity_states
                    if s.entity_id in t.signal_entity_ids
                ),
                default=None,
            )
            if signal_since is None:
                continue
            # A signal ``last_changed`` from boot until ~startup completion is a
            # boot artifact, not the true transition, and can't be recorder-
            # reconstructed: seed an unlocked lower bound from boot. A drop after
            # startup completed is trusted exactly. The upper edge is startup
            # completion (not a fixed boot+window) since integrations set up at
            # staggered times.
            window = timedelta(seconds=RESTART_ARTIFACT_WINDOW_SECONDS)
            if boot_time is not None and signal_since >= boot_time - window:
                started_edge = (ha_started_at or boot_time) + window
                if ha_started and signal_since > started_edge:
                    # A genuine drop after startup completed → trust exactly.
                    downtime[did] = DowntimeRecord(
                        since=signal_since,
                        is_lower_bound=False,
                        recorder_resolved=True,
                    )
                else:
                    # Still starting, or established during the startup window.
                    downtime[did] = DowntimeRecord(
                        since=boot_time,
                        is_lower_bound=True,
                        recorder_resolved=False,
                    )
            elif boot_time is not None:
                # signal_since predates the restart band, so it is NOT a this-boot
                # artifact — trust it as a real transition (resolved, so the
                # proven-dead-before-boot path fires the true outage immediately)
                # but mark it a lower bound, since a signal-only device's transition
                # can't be recorder-verified.
                downtime[did] = DowntimeRecord(
                    since=signal_since,
                    is_lower_bound=True,
                    recorder_resolved=True,
                )
            else:
                # No boot context (Vigil installed after boot) — trust exactly.
                downtime[did] = DowntimeRecord(
                    since=signal_since,
                    is_lower_bound=False,
                    recorder_resolved=True,
                )
