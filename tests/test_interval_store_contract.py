# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The interval-store contract, exercised against the SQLAlchemy backend."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vigil.persistence import (
    FlushSet,
    IntervalStoreError,
    IntervalStoreProtocol,
)
from custom_components.vigil.persistence.sqlalchemy_backend import (
    SQLAlchemyIntervalStore,
)

UTC = timezone.utc

StoreFactory = Callable[[], IntervalStoreProtocol]


@pytest.fixture
def make_store(hass: HomeAssistant, tmp_path: Path) -> StoreFactory:
    """Build fresh store instances all pointing at the same local SQLite DB."""
    url = f"sqlite:///{tmp_path / 'intervals.db'}"
    return lambda: SQLAlchemyIntervalStore(hass, url)


@pytest.fixture
def make_broken_store(hass: HomeAssistant, tmp_path: Path) -> StoreFactory:
    """A store whose backing DB cannot be opened: a FILE sits where a directory is
    expected, so even the mkdir of the parent fails."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    url = f"sqlite:///{blocker / 'sub' / 'intervals.db'}"
    return lambda: SQLAlchemyIntervalStore(hass, url)


async def test_broken_store_load_raises(make_broken_store: StoreFactory) -> None:
    """A load failure raises IntervalStoreError on every backend."""
    with pytest.raises(IntervalStoreError):
        await make_broken_store().async_load()


async def test_broken_store_flush_returns_false(
    make_broken_store: StoreFactory,
) -> None:
    """A write failure returns False (never raises) on every backend."""
    now = datetime(2026, 7, 1, tzinfo=UTC)
    ok = await make_broken_store().async_flush(FlushSet(seen={"s.a": (now, now)}))
    assert ok is False


async def test_sqlite_corrupt_seen_timestamp_does_not_crash_load(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A corrupt/unparsable seen timestamp must not crash load; the entity is absent."""
    path = str(tmp_path / "corrupt.db")
    url = f"sqlite:///{path}"
    now = datetime(2026, 7, 1, tzinfo=UTC)
    store = SQLAlchemyIntervalStore(hass, url)
    await store.async_load()
    await store.async_flush(FlushSet(seen={"s.good": (now, now), "s.bad": (now, now)}))

    conn = sqlite3.connect(path)
    conn.execute("UPDATE seen SET first='not-a-timestamp' WHERE entity_id='s.bad'")
    conn.commit()
    conn.close()

    state = await SQLAlchemyIntervalStore(hass, url).async_load()  # must not raise
    assert "s.good" in state.first_seen
    assert "s.bad" not in state.first_seen  # unparsable → absent, not None


async def test_load_empty_creates_schema(make_store: StoreFactory) -> None:
    state = await make_store().async_load()
    assert state.daily_max == {}
    assert state.first_seen == {}
    assert state.watermark is None


async def test_flush_and_reload_roundtrip(make_store: StoreFactory) -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    first = datetime(2026, 6, 1, tzinfo=UTC)
    store = make_store()
    await store.async_load()
    await store.async_flush(
        FlushSet(
            buckets={
                ("sensor.a", 739800): 60.0,
                ("sensor.a", 739801): 120.0,
                ("sensor.b", 739801): 30.0,
            },
            seen={"sensor.a": (first, now), "sensor.b": (now, now)},
            watermark=now,
        )
    )
    # A fresh store instance reads it back identically.
    state = await make_store().async_load()
    assert state.daily_max["sensor.a"] == {739800: 60.0, 739801: 120.0}
    assert state.daily_max["sensor.b"] == {739801: 30.0}
    assert state.first_seen["sensor.a"] == first
    assert state.last_seen["sensor.a"] == now
    assert state.watermark == now


async def test_flush_upsert_overwrites_bucket(make_store: StoreFactory) -> None:
    store = make_store()
    await store.async_load()
    await store.async_flush(FlushSet(buckets={("s.a", 100): 10.0}))
    await store.async_flush(FlushSet(buckets={("s.a", 100): 99.0}))  # same key
    state = await make_store().async_load()
    assert state.daily_max["s.a"] == {100: 99.0}


async def test_flush_delete_removes_entity(make_store: StoreFactory) -> None:
    store = make_store()
    await store.async_load()
    now = datetime(2026, 7, 1, tzinfo=UTC)
    await store.async_flush(
        FlushSet(
            buckets={("s.gone", 1): 5.0, ("s.keep", 1): 5.0},
            seen={"s.gone": (now, now), "s.keep": (now, now)},
        )
    )
    await store.async_flush(FlushSet(deleted={"s.gone"}))
    state = await make_store().async_load()
    assert "s.gone" not in state.daily_max
    assert "s.gone" not in state.first_seen
    assert "s.keep" in state.daily_max


async def test_flush_prune_drops_old_days(make_store: StoreFactory) -> None:
    store = make_store()
    await store.async_load()
    await store.async_flush(
        FlushSet(buckets={("s.a", 100): 1.0, ("s.a", 200): 2.0, ("s.a", 300): 3.0})
    )
    await store.async_flush(FlushSet(prune_before_day=200))
    state = await make_store().async_load()
    assert state.daily_max["s.a"] == {200: 2.0, 300: 3.0}  # day 100 pruned


async def test_empty_flush_is_noop(make_store: StoreFactory) -> None:
    store = make_store()
    await store.async_load()
    await store.async_flush(FlushSet())  # must not raise / touch anything
    state = await make_store().async_load()
    assert state.daily_max == {}


async def test_state_kv_roundtrip(make_store: StoreFactory) -> None:
    """The shared key-value ``state`` table round-trips JSON on both backends: a
    missing key is None, save/reload returns it, and upsert overwrites."""
    store = make_store()
    await store.async_load()
    assert await store.async_load_state("faults") is None
    await store.async_save_state("faults", {"a": [1, 2], "b": "x"})
    assert await make_store().async_load_state("faults") == {"a": [1, 2], "b": "x"}
    await store.async_save_state("faults", {"a": [3]})  # overwrite
    assert await make_store().async_load_state("faults") == {"a": [3]}


async def test_state_kv_is_best_effort(make_broken_store: StoreFactory) -> None:
    """On an unreachable store, state load returns None and save is swallowed —
    fault/ack state is re-derivable, so it must never break setup or a cycle."""
    store = make_broken_store()
    assert await store.async_load_state("faults") is None
    await store.async_save_state("faults", {"x": 1})  # must not raise


async def test_store_repo_retries_until_write_is_confirmed(
    hass: HomeAssistant,
) -> None:
    """StoreRepo advances its saved-marker only on a CONFIRMED write, so a
    swallowed failure is retried next cycle (even with an unchanged value) rather
    than silently assumed-saved and lost on restart."""
    from custom_components.vigil.storage import StoreRepo

    saves: list[object] = []
    succeed = {"ok": False}

    class _FlakyStore:
        async def async_load_state(self, key: str) -> object | None:
            return None

        async def async_save_state(self, key: str, value: object) -> bool:
            saves.append(value)
            return succeed["ok"]

    repo: StoreRepo[dict[str, int]] = StoreRepo(
        hass,
        _FlakyStore(),
        key="faults",
        initial={},
        serialize=lambda v: dict(v),
        deserialize=lambda d: dict(d) if isinstance(d, dict) else {},
    )
    repo._value = {"a": 1}

    repo.persist()  # write fails (swallowed) → marker NOT advanced
    await hass.async_block_till_done()
    assert len(saves) == 1

    repo.persist()  # same value, but last write failed → RETRY
    await hass.async_block_till_done()
    assert len(saves) == 2  # the fix: retried despite unchanged value

    succeed["ok"] = True
    repo.persist()  # now succeeds → marker advances
    await hass.async_block_till_done()
    assert len(saves) == 3

    repo.persist()  # unchanged + confirmed saved → no redundant write
    await hass.async_block_till_done()
    assert len(saves) == 3
