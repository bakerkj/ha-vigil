# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from homeassistant.core import HomeAssistant

from ..const import (
    LEARN_HORIZON_DAYS,
    LEARN_INTERVAL_PERCENTILE,
    LEARN_WARMUP_DAYS,
    heuristic_interval,
)
from ..persistence import FlushSet, IntervalStoreProtocol, create_interval_store


class IntervalLearner:
    """Learns a per-entity expected update interval over a multi-day time horizon.

    For each entity it keeps, per calendar day, the longest inter-report gap seen
    that day (a "daily max"), for the last ``LEARN_HORIZON_DAYS`` days. The
    expected interval is a high percentile of those daily maxes — the device's
    normal longest gap (e.g. a motion sensor's overnight quiet) with a single
    anomalous day dropped.

    ``observe`` only mutates memory and records which (entity, day) buckets are
    dirty; the coordinator calls :meth:`async_flush` once per cycle to persist
    just those rows via the interval store.
    """

    def __init__(
        self, hass: HomeAssistant, store: IntervalStoreProtocol | None = None
    ) -> None:
        # Injectable so an install can point the learner at a MariaDB backend;
        # defaults (empty options) to the local SQLite file.
        self._store: IntervalStoreProtocol = store or create_interval_store(hass, {})
        self._first_seen: dict[str, datetime] = {}
        self._last_seen: dict[str, datetime] = {}
        # entity_id -> {day ordinal: longest inter-report gap (seconds) that day}
        self._daily_max: dict[str, dict[int, float]] = {}
        # Point up to which recorder history has been folded in (None = never
        # seeded, so a fresh install still needs the one-time bootstrap).
        self._watermark: datetime | None = None
        # pending persistence, accumulated between flushes
        self._dirty_buckets: set[tuple[str, int]] = set()
        self._dirty_seen: set[str] = set()
        self._deleted: set[str] = set()
        # day ordinal of the last horizon prune (prune only on a day rollover)
        self._last_prune_day: int | None = None
        # Serialize flushes so the background recorder-seed flush and a per-cycle
        # flush can't interleave across their awaits on this shared state.
        self._flush_lock = asyncio.Lock()

    @property
    def store(self) -> IntervalStoreProtocol:
        """The interval-store backend, shared with the fault/ack/downtime repos so
        all Vigil state persists to the one configured store."""
        return self._store

    async def async_load(self) -> None:
        """Rehydrate persisted per-entity daily-max buckets from SQLite."""
        state = await self._store.async_load()
        self._first_seen = state.first_seen
        self._last_seen = state.last_seen
        self._daily_max = state.daily_max
        self._watermark = state.watermark
        # Ensure every seen entity has a bucket dict so observe() can extend it.
        for entity_id in self._first_seen:
            self._daily_max.setdefault(entity_id, {})

    def watermark(self) -> datetime | None:
        """Recorder point folded in so far, or None if never seeded."""
        return self._watermark

    def known_entities(self) -> list[str]:
        """Entities the learner is tracking (the set to seed/catch-up from the
        recorder)."""
        return list(self._first_seen)

    def daily_max(self, entity_id: str) -> Mapping[int, float]:
        """Per-day longest-gap buckets for an entity (read-only view)."""
        return MappingProxyType(self._daily_max.get(entity_id, {}))

    def ingest(
        self,
        buckets: dict[tuple[str, int], float],
        last_good: dict[str, datetime],
        now: datetime,
    ) -> None:
        """Merge a recorder aggregate into memory and mark it dirty for flush.

        Used for both the one-time bootstrap and every boot's catch-up. Merges
        MAX per day so it composes with live learning, backdates first_seen to the
        earliest scanned day, advances the watermark, and marks just the rows that
        actually changed dirty.
        """
        spans: dict[str, tuple[int, int]] = {}
        for (eid, day), gap in buckets.items():
            day_map = self._daily_max.setdefault(eid, {})
            if gap > day_map.get(day, 0.0):
                day_map[day] = gap
                self._dirty_buckets.add((eid, day))
            lo, hi = spans.get(eid, (day, day))
            spans[eid] = (min(lo, day), max(hi, day))
        for eid, (lo, hi) in spans.items():
            first = datetime.fromordinal(lo).replace(tzinfo=timezone.utc)
            last = datetime.fromordinal(hi).replace(tzinfo=timezone.utc)
            # Prefer the true last-good report time over midnight of the last
            # scanned day, so the first live observe doesn't fold a spuriously
            # large gap into today's bucket.
            good = last_good.get(eid)
            if good is not None and good > last:
                last = good
            changed = False
            if eid not in self._first_seen or first < self._first_seen[eid]:
                self._first_seen[eid] = first
                changed = True
            if eid not in self._last_seen or last > self._last_seen[eid]:
                self._last_seen[eid] = last
                changed = True
            if changed:
                self._dirty_seen.add(eid)
        if self._watermark is None or now > self._watermark:
            self._watermark = now
        self._last_prune_day = now.toordinal()

    def observe(self, entity_id: str, last_updated: datetime) -> None:
        """Record a report at ``last_updated`` (memory only; flush persists it).

        On a strictly-newer report, fold the inter-report gap into that day's max;
        equal/older timestamps are ignored. Only a new entity or a changed
        daily-max marks the entity's seen-row dirty.
        """
        is_new = entity_id not in self._first_seen
        if is_new:
            self._first_seen[entity_id] = last_updated
        prev = self._last_seen.get(entity_id)
        bucket_changed = False
        if prev is not None and last_updated > prev:
            gap = (last_updated - prev).total_seconds()
            day = last_updated.toordinal()
            buckets = self._daily_max.setdefault(entity_id, {})
            if gap > buckets.get(day, 0.0):
                buckets[day] = gap
                self._dirty_buckets.add((entity_id, day))
                bucket_changed = True
        if prev is None or last_updated > prev:
            self._last_seen[entity_id] = last_updated
        # Persist first/last alongside a real bucket change (or first sighting),
        # not on every routine report.
        if is_new or bucket_changed:
            self._dirty_seen.add(entity_id)

    def purge_absent(self, live_entity_ids: set[str]) -> None:
        """Forget learned state for entities that no longer exist.

        ``live_entity_ids`` is the set Vigil currently observes; a present-but-
        silent entity is still in that set, so only entities that have actually
        disappeared are purged.
        """
        gone = [eid for eid in self._last_seen if eid not in live_entity_ids]
        for eid in gone:
            self._first_seen.pop(eid, None)
            self._last_seen.pop(eid, None)
            self._daily_max.pop(eid, None)
            self._dirty_buckets = {k for k in self._dirty_buckets if k[0] != eid}
            self._dirty_seen.discard(eid)
            self._deleted.add(eid)

    async def async_flush(self, now: datetime) -> None:
        """Persist dirty buckets/seen-rows and, on a day rollover, prune the horizon.

        Called once per coordinator cycle; a no-op flush costs nothing. The lock
        serializes concurrent flushes (per-cycle vs the background recorder seed).
        """
        async with self._flush_lock:
            prune_before_day: int | None = None
            today = now.toordinal()
            do_prune = today != self._last_prune_day
            if do_prune:
                cutoff = today - LEARN_HORIZON_DAYS
                for buckets in self._daily_max.values():
                    for old in [d for d in buckets if d < cutoff]:
                        del buckets[old]
                prune_before_day = cutoff
            # A re-observed entity (dirty again) must not be deleted this cycle.
            deleted = self._deleted - self._dirty_seen
            changes = FlushSet(
                buckets={
                    (eid, day): self._daily_max[eid][day]
                    for eid, day in self._dirty_buckets
                    if day in self._daily_max.get(eid, {})
                },
                seen={
                    eid: (self._first_seen[eid], self._last_seen[eid])
                    for eid in self._dirty_seen
                    if eid in self._first_seen
                },
                deleted=deleted,
                prune_before_day=prune_before_day,
            )
            # Advance the recorder watermark alongside real work once seeded; skip
            # it on a genuinely empty cycle so an idle flush stays free.
            advance_watermark = self._watermark is not None and not changes.is_empty()
            if advance_watermark:
                changes.watermark = now
            # Clear the dirty markers for what we're about to persist BEFORE the
            # await: ``changes`` already captured the values, so a concurrent
            # observe()/ingest() touching an already-flushed bucket re-marks it
            # dirty (persisted next cycle) instead of being lost to a post-await
            # subtract of a key whose value moved on.
            flushed_buckets = set(self._dirty_buckets)
            flushed_seen = set(self._dirty_seen)
            flushed_deleted = set(self._deleted)
            self._dirty_buckets -= flushed_buckets
            self._dirty_seen -= flushed_seen
            self._deleted -= flushed_deleted
            # Only finalize once the write is confirmed persisted. A failed flush
            # restores every dirty marker and leaves the watermark in place, so the
            # batch is retried next cycle.
            if not await self._store.async_flush(changes):
                self._dirty_buckets |= flushed_buckets
                self._dirty_seen |= flushed_seen
                self._deleted |= flushed_deleted
                return
            if advance_watermark:
                self._watermark = now
            if do_prune:
                self._last_prune_day = today

    async def async_close(self) -> None:
        """Release the store's resources (e.g. a DB pool) on unload."""
        await self._store.async_close()

    def is_populated(self, entity_id: str) -> bool:
        """True once the entity has been observed for a full warmup and has at
        least one day-bucket, so the expected interval reflects a real cadence.

        A single bucket is deliberately enough: a sparsely-reporting entity has one
        bucket but a real cadence, and holding it to the heuristic risks a false
        positive. The lone-restart-artifact bucket case is guarded upstream (the
        recorder seed rejects restart-artifact gaps before they become buckets).
        """
        first = self._first_seen.get(entity_id)
        last = self._last_seen.get(entity_id)
        if first is None or last is None or not self._daily_max.get(entity_id):
            return False
        return (last - first) >= timedelta(days=LEARN_WARMUP_DAYS)

    def learned_interval(self, entity_id: str) -> float | None:
        """A high percentile (nearest-rank) of the per-day max gaps — the normal
        longest gap — or ``None`` if not yet populated.

        Always drops at least the single largest day (the ``n - 2`` clamp) once
        there are >= 2 days, so one anomalous day can't inflate the threshold; for
        larger ``n`` the percentile drops ~the top ``1 - p`` fraction.
        """
        if not self.is_populated(entity_id):
            return None
        # is_populated guarantees a non-empty daily-max dict, so maxes is non-empty.
        maxes = sorted(self._daily_max[entity_id].values())
        n = len(maxes)
        index = max(0, min(n - 2, math.ceil(LEARN_INTERVAL_PERCENTILE * n) - 1))
        return maxes[index]

    def expected_interval(
        self, entity_id: str, domain: str, device_class: str | None
    ) -> float:
        """Learned interval when populated, else the domain/device_class heuristic."""
        learned = self.learned_interval(entity_id)
        if learned is not None:
            return learned
        return heuristic_interval(domain, device_class)
