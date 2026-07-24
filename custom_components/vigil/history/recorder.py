# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Recorder access for the coordinator.

All of Vigil's reads against the HA recorder database live here as free functions
taking ``hass`` — so the coordinator holds only the orchestration (which devices
to query, how to fold the results into its cross-cycle state), not the SQL.

* :func:`async_recorder_last_good` — reconstruct each offline device's outage
  start from recorder history (used by Engine 2 to survive restarts).
* :func:`select_downtime` — the pure, artifact-rejecting selection it delegates to.
* :func:`async_recorder_interval_aggregate` — the server-side per-(entity, day)
  MAX inter-report gap that seeds the interval learner (Engine 3).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from itertools import batched

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.db_schema import RecorderRuns
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.models import process_timestamp
from homeassistant.components.recorder.util import (  # type: ignore[attr-defined]
    session_scope,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from sqlalchemy import bindparam, text

from ..const import (
    RECORDER_ENTITY_CHUNK,
    RECORDER_NON_VALUE_STATES,
    RESTART_ARTIFACT_WINDOW_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


def select_downtime(
    history: dict[str, list[State | dict[str, object]]],
    entity_to_device: dict[str, str],
    boot_time: datetime | None,
    run_starts: list[datetime],
    window: timedelta,
    start: datetime,
) -> tuple[dict[str, datetime], set[str], set[str]]:
    """Reconstruct each device's outage start from recorder history.

    Returns ``(offline_since per device, floored ids, recorder-blind ids)``. A
    "good" state is rejected as a restart artifact when at/after ``boot_time`` or
    within ``window`` of any ``run_starts`` entry (a value RESTORED around a
    restart), distinguishing a restore from a real reading.

    Per offline device:
      * any real (non-artifact) good value → that timestamp;
      * rows exist but none good → floored to ``max(start, earliest_evidence_row)``,
        a lower bound (dead at least as far back as the recorder has evidence). The
        recorder synthesizes a ``last_changed == start`` edge state only under full
        retention, so the oldest row equals ``start`` and the floor reads ">= window";
        a per-entity trim earlier than the window has no synthetic edge, so the oldest
        row is the real trim edge inside the window and the floor honestly reads ">= R"
        (R < window) — absence of older data is not evidence of a longer outage;
      * no rows at all → recorder-BLIND (third set, not ``result``): no evidence of an
        outage, so flooring would stamp a phantom ">= window" on a live device.

    An offline device with rows but no trustworthy reading is ALWAYS floored and
    flagged (missing a dead device is worse than briefly flagging a new one); the
    blind case is distinct so a recorder-excluded-but-live entity is never floored.
    """

    def _is_restart_artifact(ts: datetime) -> bool:
        if boot_time is not None and ts >= boot_time:
            return True
        # Symmetric window: a value restored at a restart can land slightly
        # before the recorded run start (entities restore around the boot, not
        # exactly at the logged instant), so reject within ``window`` either side.
        return any(rs - window <= ts <= rs + window for rs in run_starts)

    entities_by_device: dict[str, list[str]] = {}
    for entity_id, device_id in entity_to_device.items():
        entities_by_device.setdefault(device_id, []).append(entity_id)

    result: dict[str, datetime] = {}
    floored: set[str] = set()
    blind: set[str] = set()
    for device_id, entity_ids in entities_by_device.items():
        real_goods: list[datetime] = []
        # earliest_row stays None iff the recorder has ZERO rows for every one of
        # this device's data entities (recorder-blind). Any rows at all (even
        # all-unavailable) set it, so a no-good device WITH rows floors while a
        # no-rows device is blind.
        earliest_row: datetime | None = None
        for entity_id in entity_ids:
            seq = [s for s in history.get(entity_id, []) if isinstance(s, State)]
            if not seq:
                continue
            # Oldest evidence across the device's data entities: the edge of what
            # the recorder has. Rows come back in ascending time order, so seq[0]
            # is this entity's oldest — ``start`` for a full-retention entity (the
            # synthetic edge state), the real (later) trim edge for a trimmed one.
            first_ts = seq[0].last_changed
            if earliest_row is None or first_ts < earliest_row:
                earliest_row = first_ts
            good: datetime | None = None
            for i in range(len(seq) - 1, -1, -1):
                state = seq[i]
                if state.state in RECORDER_NON_VALUE_STATES:
                    continue
                # Reject a "good" value as a restart-restore ONLY when both at a
                # restart instant AND isolated (preceded by a non-value state — a
                # lone restore blip on a dead device). A normally-reporting device
                # has CONSECUTIVE good values, so its readings are trusted; this
                # stops integrations that publish many sensors in lockstep (e.g.
                # Frigate's cameras) from flooring to ">= window" on a brief
                # unavailable.
                isolated = i > 0 and seq[i - 1].state in RECORDER_NON_VALUE_STATES
                if isolated and _is_restart_artifact(state.last_changed):
                    continue
                good = state.last_changed
                break
            if good is not None:
                real_goods.append(good)
        if real_goods:
            # A real reading wins; device "went down" when its last-still-
            # reporting entity stopped.
            result[device_id] = max(real_goods)
        elif earliest_row is not None:
            # Rows exist but none good → dead at least as far back as the recorder
            # has evidence; floor to the oldest row (window edge under full
            # retention, real trim edge otherwise). ``max`` guards the degenerate
            # case where every row sat exactly at ``start``.
            result[device_id] = max(start, earliest_row)
            floored.add(device_id)
        else:
            # No rows at all — recorder is BLIND (entities recorder-excluded or
            # never recorded). No evidence of an outage, so do NOT floor; the
            # caller leaves the record unseeded and Engine 2 uses the live
            # offline_since + normal grace instead.
            blind.add(device_id)
    return result, floored, blind


async def async_recorder_last_good(
    hass: HomeAssistant,
    entity_to_device: dict[str, str],
    boot_time: datetime | None,
    now: datetime,
    lookback: timedelta,
) -> tuple[dict[str, datetime], set[str], set[str], bool]:
    """Per-device outage start, the floored set, the recorder-blind set, and
    whether run history was readable.

    Fetches recorder history (chunked) and HA restart instants (recorder run
    starts), then delegates the artifact-rejecting selection to
    :func:`select_downtime`. ``run_history_ok`` is False if the run-history read
    failed, so the caller can avoid locking in a possibly-wrong value.
    """
    # ``start`` is derived from the cycle ``now`` (threaded in), so the floored
    # window edge matches Engine 2's view to the second — no sub-second drift.
    start = now - lookback
    window = timedelta(seconds=RESTART_ARTIFACT_WINDOW_SECONDS)
    entity_ids = list(entity_to_device)

    def _query() -> tuple[
        dict[str, list[State | dict[str, object]]], list[datetime], bool
    ]:
        history: dict[str, list[State | dict[str, object]]] = {}
        for batch in batched(entity_ids, RECORDER_ENTITY_CHUNK):
            history.update(
                get_significant_states(
                    hass,
                    start,
                    None,
                    list(batch),
                    # MUST be False: a device dead the whole window has no
                    # state *changes*, so significant_changes_only=True omits
                    # it entirely and we can't floor it.
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=True,
                )
            )
        # HA restart instants — a value restored just after one of these is a
        # restart artifact, not a real reading.
        run_starts: list[datetime] = []
        run_history_ok = True
        try:
            with session_scope(hass=hass, read_only=True) as session:
                for (raw_start,) in session.query(RecorderRuns.start):
                    ts = process_timestamp(raw_start)
                    if ts is not None and ts >= start - window:
                        run_starts.append(ts)
        except Exception:  # run history is best-effort
            run_history_ok = False
            _LOGGER.debug("Vigil: could not read recorder run history", exc_info=True)
        return history, run_starts, run_history_ok

    try:
        history, run_starts, run_history_ok = await get_instance(
            hass
        ).async_add_executor_job(_query)
    except Exception:  # a busy/unready recorder must not blank the cycle
        # get_instance() or the history read can raise on a healthy-but-busy
        # recorder (DB locked, mid-purge/migration, disk full, not-yet-ready).
        # Degrade to "blind" for every queried device so Engine 2 falls back to
        # live offline_since + grace, and mark it unresolved so a later cycle
        # re-queries and self-corrects.
        _LOGGER.warning(
            "Vigil: recorder history query failed; treating %d device(s) as "
            "recorder-blind this cycle (live offline_since + grace)",
            len(set(entity_to_device.values())),
            exc_info=True,
        )
        return {}, set(), set(entity_to_device.values()), False
    result, floored, blind = select_downtime(
        history, entity_to_device, boot_time, run_starts, window, start
    )
    return result, floored, blind, run_history_ok


# The [start, now) window of GOOD (non-null, value-bearing) states, shared by
# both aggregate queries below so their bounds can't drift out of lockstep.
_STATES_WINDOW = (
    "metadata_id IN :mids AND last_updated_ts >= :start_ts "
    "AND last_updated_ts < :now_ts AND state IS NOT NULL AND state NOT IN :nonvalues"
)


async def async_recorder_interval_aggregate(
    hass: HomeAssistant, entity_ids: list[str], start: datetime, now: datetime
) -> tuple[dict[tuple[str, int], float], dict[str, datetime]]:
    """Per-(entity, day) MAX inter-report gap + per-entity last-good timestamp
    for states at/after ``start``, computed server-side.

    A window function does the heavy lifting in the recorder DB (one row out
    per entity-day, not every state streamed into Python). ``day`` is emitted
    as a Unix epoch day and shifted by ``date(1970,1,1).toordinal()`` to match
    the learner's ``datetime.toordinal()`` buckets. MariaDB's CAST-to-integer
    ROUNDS while SQLite truncates, so the day floor is dialect-specific. Runs
    on the recorder executor.
    """
    start_ts = start.timestamp()
    now_ts = now.timestamp()
    nonvalues = list(RECORDER_NON_VALUE_STATES)
    day_offset = date(1970, 1, 1).toordinal()
    engine = get_instance(hass).engine
    dialect = engine.dialect.name if engine is not None else "sqlite"
    day_floor = (
        "CAST(g.ts / 86400 AS INTEGER)"
        if dialect == "sqlite"
        else "FLOOR(g.ts / 86400)"
    )
    # Gaps are measured between consecutive GOOD states (the same unavailable/
    # unknown filter engine3 applies before observe); otherwise an unavailable row
    # would split a real overnight gap into two smaller ones and tighten the
    # threshold. This is state-CHANGE cadence, not report cadence — the recorder
    # stores a row only on a change, whereas live observe() uses last_reported
    # (advances on every re-report). So for steady-value sensors the seed's gaps
    # run LARGER than live's; the per-day MAX merge keeps the larger value, biasing
    # the learned interval conservatively until the seed buckets age past the
    # horizon (the recorder cannot supply report cadence).
    # Bound the window at BOTH edges [start, now): the upper bound keeps a
    # clock-skewed / future-dated row out of a future day and honors the ``now``
    # the caller threads in for a consistent cycle view.
    # The f-string interpolations below (``day_floor``, ``_STATES_WINDOW``) are
    # module-local literals, never user input; every value is a bound parameter.
    gap_sql = text(
        f"SELECT g.mid, {day_floor} AS day, MAX(g.gap) "
        "FROM (SELECT metadata_id AS mid, last_updated_ts AS ts, "
        "  last_updated_ts - LAG(last_updated_ts) OVER ("
        "    PARTITION BY metadata_id ORDER BY last_updated_ts) AS gap "
        "  FROM states "
        f"  WHERE {_STATES_WINDOW}) g "
        "WHERE g.gap IS NOT NULL GROUP BY g.mid, day"
    ).bindparams(
        bindparam("mids", expanding=True),
        bindparam("nonvalues", expanding=True),
    )
    good_sql = text(
        f"SELECT metadata_id, MAX(last_updated_ts) FROM states WHERE {_STATES_WINDOW} "
        "GROUP BY metadata_id"
    ).bindparams(
        bindparam("mids", expanding=True),
        bindparam("nonvalues", expanding=True),
    )

    def _query() -> tuple[dict[tuple[str, int], float], dict[str, datetime]]:
        buckets: dict[tuple[str, int], float] = {}
        last_good: dict[str, datetime] = {}
        with session_scope(hass=hass, read_only=True) as session:
            for chunk in batched(entity_ids, RECORDER_ENTITY_CHUNK):
                id_to_entity = {
                    mid: eid
                    for mid, eid in session.execute(
                        text(
                            "SELECT metadata_id, entity_id FROM states_meta "
                            "WHERE entity_id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": chunk},
                    )
                }
                if not id_to_entity:
                    continue
                mids = list(id_to_entity)
                params = {
                    "mids": mids,
                    "start_ts": start_ts,
                    "now_ts": now_ts,
                    "nonvalues": nonvalues,
                }
                for mid, day, max_gap in session.execute(gap_sql, params):
                    buckets[(id_to_entity[mid], int(day) + day_offset)] = float(max_gap)
                for mid, ts in session.execute(good_sql, params):
                    if ts is not None:
                        last_good[id_to_entity[mid]] = dt_util.utc_from_timestamp(
                            float(ts)
                        )
        return buckets, last_good

    return await get_instance(hass).async_add_executor_job(_query)
