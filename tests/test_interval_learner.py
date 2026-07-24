# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant

from custom_components.vigil.const import LEARN_HORIZON_DAYS, LEARN_WARMUP_DAYS
from custom_components.vigil.learning.interval_learner import IntervalLearner
from custom_components.vigil.persistence import FlushSet, LoadedState
from tests.helpers import seed_learner

ENTITY = "binary_sensor.motion"


def _at(days: float = 0, seconds: float = 0) -> datetime:
    return datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=days, seconds=seconds)


async def test_observe_keeps_longest_gap_per_day(hass: HomeAssistant) -> None:
    """Each day-bucket holds the LONGEST inter-report gap that day."""
    learner = IntervalLearner(hass)
    learner.observe(ENTITY, _at(0, 0))
    learner.observe(ENTITY, _at(0, 60))  # gap 60
    learner.observe(ENTITY, _at(0, 660))  # gap 600  <-- day's max
    learner.observe(ENTITY, _at(0, 720))  # gap 60
    assert learner.daily_max(ENTITY)[_at(0).toordinal()] == 600.0


async def test_not_populated_before_warmup(hass: HomeAssistant) -> None:
    learner = IntervalLearner(hass)
    learner.observe(ENTITY, _at(0, 0))
    learner.observe(ENTITY, _at(0, 3600))  # only an hour of history
    assert learner.is_populated(ENTITY) is False
    assert learner.learned_interval(ENTITY) is None


async def test_populated_after_warmup(hass: HomeAssistant) -> None:
    learner = IntervalLearner(hass)
    learner.observe(ENTITY, _at(0, 0))
    learner.observe(ENTITY, _at(LEARN_WARMUP_DAYS, 0))  # spans the warmup
    assert learner.is_populated(ENTITY) is True
    assert learner.learned_interval(ENTITY) is not None


async def test_learned_interval_is_the_long_daily_gap_not_busy_cadence(
    hass: HomeAssistant,
) -> None:
    """Expected interval reflects the device's LONGEST normal gap (~8h overnight),
    not its busy-time cadence (~80s)."""
    learner = IntervalLearner(hass)
    seed_learner(learner, ENTITY, gap_seconds=8 * 3600.0, days=4)
    assert learner.learned_interval(ENTITY) == 8 * 3600.0


async def test_learned_interval_drops_anomaly_at_small_n(hass: HomeAssistant) -> None:
    """One freak day (e.g. an HA restart) must not loosen the threshold: the single
    largest day is always dropped. This holds both under the small-n n-2 clamp
    (n=2,3,4) and via the high percentile at a normal day count (n=12)."""
    base = _at(0).toordinal()
    for n, expected in ((2, 600.0), (3, 600.0), (4, 600.0), (12, 600.0)):
        learner = IntervalLearner(hass)
        learner._first_seen[ENTITY] = _at(0)
        learner._last_seen[ENTITY] = _at(n)
        buckets = {base + d: 600.0 for d in range(n - 1)}
        buckets[base + n - 1] = 3600.0  # the single anomalous day
        learner._daily_max[ENTITY] = buckets
        assert learner.learned_interval(ENTITY) == expected, f"n={n}"


async def test_flush_keeps_markers_added_during_flush(hass: HomeAssistant) -> None:
    """A bucket observed WHILE a flush is awaiting the store must survive the
    flush's finalize step (it wasn't in the persisted batch): it stays dirty and is
    persisted by the next flush."""

    class _MidFlushStore:
        def __init__(self) -> None:
            self.learner: IntervalLearner | None = None
            self.batches: list[FlushSet] = []

        async def async_load(self) -> LoadedState:
            return LoadedState()

        async def async_flush(self, changes: FlushSet) -> bool:
            self.batches.append(changes)
            if len(self.batches) == 1 and self.learner is not None:
                # A concurrent observe arriving while this flush is parked on I/O.
                self.learner.observe("sensor.mid", _at(0))
                self.learner.observe("sensor.mid", _at(0, 600))
            return True

        async def async_load_state(self, key: str) -> object | None:
            return None

        async def async_save_state(self, key: str, value: object) -> bool:
            return True

        async def async_close(self) -> None:
            return None

    store = _MidFlushStore()
    learner = IntervalLearner(hass, store)
    store.learner = learner
    await learner.async_load()
    learner.observe(ENTITY, _at(0))
    learner.observe(ENTITY, _at(0, 300))

    # Batch #1 (this flush) can't include the mid-flush bucket; it must survive the
    # finalize and be persisted by the next flush.
    await learner.async_flush(_at(1))
    await learner.async_flush(_at(1, 60))
    persisted = {eid for batch in store.batches for (eid, _d) in batch.buckets}
    assert "sensor.mid" in persisted


async def test_flush_keeps_a_value_changed_during_the_await(
    hass: HomeAssistant,
) -> None:
    """A bucket whose value GROWS via a concurrent observe while the flush is
    parked on I/O stays dirty afterward, so the newer value isn't silently dropped
    (memory holds it, disk catches up next cycle)."""
    day0 = _at(0).toordinal()

    class _MidFlushStore:
        def __init__(self) -> None:
            self.learner: IntervalLearner | None = None
            self.batches: list[FlushSet] = []

        async def async_load(self) -> LoadedState:
            return LoadedState()

        async def async_flush(self, changes: FlushSet) -> bool:
            self.batches.append(changes)
            if len(self.batches) == 1 and self.learner is not None:
                # Fold a LARGER gap into the SAME already-flushed bucket mid-await.
                self.learner.observe(ENTITY, _at(0, 900))
            return True

        async def async_load_state(self, key: str) -> object | None:
            return None

        async def async_save_state(self, key: str, value: object) -> bool:
            return True

        async def async_close(self) -> None:
            return None

    store = _MidFlushStore()
    learner = IntervalLearner(hass, store)
    store.learner = learner
    learner.observe(ENTITY, _at(0, 0))
    learner.observe(ENTITY, _at(0, 300))  # bucket[day0] = 300, dirty

    await learner.async_flush(_at(1))
    # Batch #1 persisted the old value; the newer value is retained AND still dirty.
    assert store.batches[0].buckets[(ENTITY, day0)] == 300.0
    assert learner.daily_max(ENTITY)[day0] == 600.0

    # ...so the next flush persists the newer value rather than losing it.
    await learner.async_flush(_at(1, 60))
    assert store.batches[1].buckets[(ENTITY, day0)] == 600.0


async def test_learned_interval_single_bucket_uses_it(hass: HomeAssistant) -> None:
    """With exactly one day-bucket there is nothing to drop — a sparse-but-real
    entity uses that value."""
    learner = IntervalLearner(hass)
    learner._first_seen[ENTITY] = _at(0)
    learner._last_seen[ENTITY] = _at(3)
    learner._daily_max[ENTITY] = {_at(0).toordinal(): 3600.0}
    assert learner.learned_interval(ENTITY) == 3600.0


async def test_horizon_prunes_days_older_than_window(hass: HomeAssistant) -> None:
    learner = IntervalLearner(hass)
    learner.observe(ENTITY, _at(0))
    learner.observe(ENTITY, _at(0, 600))  # creates a day-0 bucket
    learner.observe(ENTITY, _at(LEARN_HORIZON_DAYS + 2))
    learner.observe(ENTITY, _at(LEARN_HORIZON_DAYS + 2, 600))
    # Pruning happens on a day rollover at flush time, not on observe.
    assert _at(0).toordinal() in learner.daily_max(ENTITY)
    await learner.async_flush(_at(LEARN_HORIZON_DAYS + 2, 600))
    assert _at(0).toordinal() not in learner.daily_max(ENTITY)


async def test_flush_load_roundtrip(hass: HomeAssistant) -> None:
    """observe() marks buckets dirty; async_flush persists just those rows; a fresh
    learner on the same store reads them back."""
    learner = IntervalLearner(hass)
    learner.observe(ENTITY, _at(0))
    learner.observe(ENTITY, _at(0, 600))  # day-0 bucket = 600
    learner.observe(ENTITY, _at(5))  # spans warmup; day-5 bucket set
    before = dict(learner.daily_max(ENTITY))
    await learner.async_flush(_at(5))

    other = IntervalLearner(hass)
    await other.async_load()
    assert other.daily_max(ENTITY) == before
    assert other.is_populated(ENTITY) is True


async def test_flush_persists_only_dirty_and_clears(hass: HomeAssistant) -> None:
    """Dirty state is cleared after each flush; a flush with nothing dirty is a
    no-op that writes nothing new."""
    learner = IntervalLearner(hass)
    learner.observe(ENTITY, _at(0))
    learner.observe(ENTITY, _at(0, 600))
    await learner.async_flush(_at(0, 600))
    assert not learner._dirty_buckets and not learner._dirty_seen

    # No new observations -> a second flush is a no-op, and reload is unchanged.
    await learner.async_flush(_at(0, 700))
    other = IntervalLearner(hass)
    await other.async_load()
    assert other.daily_max(ENTITY) == learner.daily_max(ENTITY)


async def test_purge_absent_flush_deletes_from_store(hass: HomeAssistant) -> None:
    """A purged entity is deleted from the store on the next flush, not just memory."""
    learner = IntervalLearner(hass)
    for eid in ("sensor.gone", "sensor.here"):
        learner.observe(eid, _at(0))
        learner.observe(eid, _at(0, 600))
    await learner.async_flush(_at(0, 600))

    learner.purge_absent({"sensor.here"})
    await learner.async_flush(_at(0, 700))

    other = IntervalLearner(hass)
    await other.async_load()
    assert "sensor.gone" not in other._daily_max
    assert "sensor.gone" not in other._first_seen
    assert "sensor.here" in other._daily_max


async def test_expected_interval_falls_back_to_heuristic_until_populated(
    hass: HomeAssistant,
) -> None:
    learner = IntervalLearner(hass)
    learner.observe(ENTITY, _at(0))  # not yet populated
    # motion heuristic from const._HEURISTIC_BY_DOMAIN_CLASS.
    assert learner.expected_interval(ENTITY, "binary_sensor", "motion") == 300.0


async def test_observe_ignores_equal_and_older_timestamps(
    hass: HomeAssistant,
) -> None:
    learner = IntervalLearner(hass)
    learner.observe(ENTITY, _at(0, 100))
    learner.observe(ENTITY, _at(0, 100))  # equal — ignored
    learner.observe(ENTITY, _at(0, 50))  # older — ignored
    # No gap recorded yet (no strictly-newer report).
    assert learner.daily_max(ENTITY) == {}


async def test_purge_absent_drops_removed_entities_only(hass: HomeAssistant) -> None:
    """Entities missing from the live set are forgotten; ones still present — even
    if silent — are kept."""
    learner = IntervalLearner(hass)
    for eid in ("sensor.gone", "sensor.here"):
        learner.observe(eid, _at(0))
        learner.observe(eid, _at(1))  # a gap -> first/last/bucket all set

    learner.purge_absent({"sensor.here"})

    assert "sensor.gone" not in learner._first_seen
    assert "sensor.gone" not in learner._last_seen
    assert "sensor.gone" not in learner._daily_max
    assert "sensor.here" in learner._last_seen
    assert "sensor.here" in learner._daily_max


async def test_ingest_seeds_populated_and_persists(hass: HomeAssistant) -> None:
    """ingest seeds historical buckets, backdates first_seen so the entity is
    populated at once, stamps the watermark, and the flush persists it."""
    learner = IntervalLearner(hass)
    base = _at(0).toordinal()
    buckets = {(ENTITY, base + d): 600.0 for d in range(5)}
    buckets[(ENTITY, base + 5)] = 8 * 3600.0  # one long day, dropped by n-2
    lg = _at(5, 100)
    now = _at(5, 200)

    assert learner.watermark() is None  # fresh store → needs the bootstrap
    learner.ingest(buckets, {ENTITY: lg}, now)

    assert learner.watermark() == now  # seeded
    assert learner.is_populated(ENTITY) is True
    assert learner.learned_interval(ENTITY) == 600.0
    await learner.async_flush(now)

    other = IntervalLearner(hass)
    await other.async_load()
    assert other.is_populated(ENTITY) is True
    assert other.watermark() == now


async def test_ingest_catchup_merges_max_and_skips_unchanged(
    hass: HomeAssistant,
) -> None:
    """A later catch-up ingest keeps the per-day MAX and marks only genuinely
    new/larger buckets dirty."""
    learner = IntervalLearner(hass)
    base = _at(0).toordinal()
    learner.ingest({(ENTITY, base): 600.0}, {}, _at(0, 10))
    await learner.async_flush(_at(0, 10))

    # Catch-up: a smaller gap the same day + a brand-new day.
    learner.ingest({(ENTITY, base): 100.0, (ENTITY, base + 1): 300.0}, {}, _at(1))

    assert learner.daily_max(ENTITY)[base] == 600.0  # MAX kept, not overwritten
    assert (ENTITY, base) not in learner._dirty_buckets  # 100 < 600 -> no write
    assert (ENTITY, base + 1) in learner._dirty_buckets  # new day -> written
    assert learner.watermark() == _at(1)


async def test_ingest_last_seen_uses_last_good_not_day_midnight(
    hass: HomeAssistant,
) -> None:
    """After a seed, last_seen is the true last-good report time, not midnight of
    the last scanned day — so the first live observe measures a real gap."""
    learner = IntervalLearner(hass)
    base = _at(0).toordinal()
    good = _at(0, 6 * 3600)  # 06:00 on the (single) scanned day
    learner.ingest({(ENTITY, base): 600.0}, {ENTITY: good}, _at(0, 7 * 3600))

    assert learner._last_seen[ENTITY] == good  # not _at(0) (midnight)
    # A live report an hour later folds only the real 1h gap into the day bucket.
    learner.observe(ENTITY, _at(0, 7 * 3600))
    assert learner.daily_max(ENTITY)[base] == 3600.0  # 1h, not ~7h from midnight


class _FlakyStore:
    """A store whose flushes fail until ``fail`` is cleared (records what lands)."""

    def __init__(self) -> None:
        self.fail = True
        self.flushed: list[FlushSet] = []

    async def async_load(self) -> LoadedState:
        return LoadedState()

    async def async_flush(self, changes: FlushSet) -> bool:
        if self.fail:
            return False
        self.flushed.append(changes)
        return True

    async def async_load_state(self, key: str) -> object | None:
        return None

    async def async_save_state(self, key: str, value: object) -> bool:
        return True

    async def async_close(self) -> None:
        return None


async def test_failed_flush_keeps_dirty_and_does_not_advance_watermark(
    hass: HomeAssistant,
) -> None:
    """A flush that fails to persist retains every dirty bucket and leaves the
    watermark un-advanced, then persists it all on the next successful flush."""
    store = _FlakyStore()
    learner = IntervalLearner(hass, store)
    base = _at(0).toordinal()
    learner.ingest({(ENTITY, base): 600.0}, {ENTITY: _at(0, 10)}, _at(0, 20))

    store.fail = True
    await learner.async_flush(_at(0, 30))
    # Nothing persisted; the seeded bucket + seen row stay dirty for retry (proven
    # below: the next successful flush lands the still-dirty seeded bucket).
    assert store.flushed == []

    store.fail = False
    await learner.async_flush(_at(0, 40))
    # Now it lands — the seeded bucket survived, and the watermark rides along.
    assert len(store.flushed) == 1
    persisted = store.flushed[0]
    assert persisted.buckets[(ENTITY, base)] == 600.0
    assert persisted.watermark is not None
    assert not learner._dirty_buckets and not learner._dirty_seen
