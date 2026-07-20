# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Backend-specific tests for the SQLAlchemy-backed interval store."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vigil.const import (
    CONF_INTERVAL_STORE_URL,
    STORAGE_SQLITE_FILE,
)
from custom_components.vigil.persistence.sqlalchemy_backend import (
    SQLAlchemyIntervalStore,
)
from custom_components.vigil.persistence import create_interval_store


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'sa_intervals.db'}"


@pytest.mark.parametrize(
    "options", [{CONF_INTERVAL_STORE_URL: ""}, {}, {CONF_INTERVAL_STORE_URL: "   "}]
)
def test_factory_blank_url_returns_local_sqlite(
    hass: HomeAssistant, options: dict[str, str]
) -> None:
    """A blank/missing/whitespace URL targets the default .storage SQLite file via
    a sqlite:/// URL on the one SQLAlchemy backend."""
    store = create_interval_store(hass, options)
    assert isinstance(store, SQLAlchemyIntervalStore)
    assert store._url.startswith("sqlite:")
    assert store._url.endswith(STORAGE_SQLITE_FILE)


def test_factory_url_returns_sqlalchemy(hass: HomeAssistant, tmp_path: Path) -> None:
    store = create_interval_store(hass, {CONF_INTERVAL_STORE_URL: _url(tmp_path)})
    assert isinstance(store, SQLAlchemyIntervalStore)


def test_target_redacts_password(hass: HomeAssistant) -> None:
    """The log identifier (``_target``) redacts the URL password."""
    store = SQLAlchemyIntervalStore(hass, "mysql+pymysql://user:secret@host/db")
    safe = store._target()
    assert "secret" not in safe
    assert "user" in safe


def test_dialect_sql_sqlite_vs_mysql() -> None:
    """Pin the SQLAlchemy-Core DDL/UPSERT output for both the sqlite and mysql dialects."""
    from sqlalchemy.dialects import mysql, sqlite
    from sqlalchemy.schema import CreateTable

    from custom_components.vigil.persistence.sqlalchemy_backend import (
        _DAILY_MAX,
        _META,
        _upsert,
    )

    def _sql(stmt: object, dialect: object) -> str:
        return str(stmt.compile(dialect=dialect))  # type: ignore[attr-defined]

    # DDL: MySQL gets VARCHAR/DOUBLE; SQLite gets REAL; ``key`` is quoted on both.
    mysql_daily_ddl = _sql(CreateTable(_DAILY_MAX), mysql.dialect())
    assert "VARCHAR(255)" in mysql_daily_ddl
    assert "DOUBLE" in mysql_daily_ddl  # not the default FLOAT
    # SQLite renders a float column as FLOAT (REAL affinity) — not MySQL's DOUBLE.
    sqlite_daily_ddl = _sql(CreateTable(_DAILY_MAX), sqlite.dialect())
    assert "FLOAT" in sqlite_daily_ddl
    assert "DOUBLE" not in sqlite_daily_ddl
    assert "`key`" in _sql(CreateTable(_META), mysql.dialect())
    assert '"key"' in _sql(CreateTable(_META), sqlite.dialect())

    # UPSERT clause differs by dialect.
    assert "ON CONFLICT" in _sql(
        _upsert(_DAILY_MAX, "sqlite", ("gap",)), sqlite.dialect()
    )
    assert "ON DUPLICATE KEY UPDATE" in _sql(
        _upsert(_DAILY_MAX, "mysql", ("gap",)), mysql.dialect()
    )
    assert "ON CONFLICT" in _sql(_upsert(_META, "sqlite", ("value",)), sqlite.dialect())
    assert "ON DUPLICATE KEY UPDATE" in _sql(
        _upsert(_META, "mysql", ("value",)), mysql.dialect()
    )


async def test_sqlite_pragmas_are_set_on_connect(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """The local-file path applies the WAL + synchronous=NORMAL tuning, verified
    on a live connection."""
    import sqlite3

    path = str(tmp_path / "pragmas.db")
    store = SQLAlchemyIntervalStore(hass, f"sqlite:///{path}")
    await store.async_load()  # opens the engine, creates the file
    await store.async_close()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


async def test_existing_sqlite_file_round_trips_through_sqlalchemy(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """An existing .storage/vigil_intervals.db (identical schema) keeps working:
    write via the SQLAlchemy backend, read it back on a fresh instance."""
    from datetime import datetime, timezone

    from custom_components.vigil.persistence import FlushSet

    url = f"sqlite:///{tmp_path / 'existing.db'}"
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    w = SQLAlchemyIntervalStore(hass, url)
    await w.async_load()
    await w.async_flush(
        FlushSet(buckets={("s.a", now.toordinal()): 42.0}, seen={"s.a": (now, now)})
    )
    await w.async_save_state("faults", {"x": 1})
    await w.async_close()

    r = SQLAlchemyIntervalStore(hass, url)
    state = await r.async_load()
    assert state.daily_max["s.a"][now.toordinal()] == 42.0
    assert state.first_seen["s.a"] == now
    assert await r.async_load_state("faults") == {"x": 1}


async def test_load_salvages_a_poison_daily_max_row(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A single unparsable daily_max row (non-numeric gap) is skipped rather than
    failing the whole load: a permanent corruption must not raise
    IntervalStoreError and retry setup forever — the rest of the store still loads."""
    import sqlite3
    from datetime import datetime, timezone

    from custom_components.vigil.persistence import FlushSet

    path = tmp_path / "poison.db"
    url = f"sqlite:///{path}"
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    w = SQLAlchemyIntervalStore(hass, url)
    await w.async_load()
    await w.async_flush(
        FlushSet(
            buckets={("s.good", now.toordinal()): 42.0}, seen={"s.good": (now, now)}
        )
    )
    await w.async_close()

    # Inject a poison bucket whose gap is a non-numeric string — the shape a
    # schema-drifted / tampered external DB produces.
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO daily_max (entity_id, day, gap) VALUES (?, ?, ?)",
            ("s.bad", now.toordinal(), "not-a-number"),
        )
        conn.commit()
    finally:
        conn.close()

    r = SQLAlchemyIntervalStore(hass, url)
    state = await r.async_load()  # must NOT raise
    await r.async_close()
    assert state.daily_max["s.good"][now.toordinal()] == 42.0  # good row survives
    assert "s.bad" not in state.daily_max  # poison row skipped


async def test_load_salvages_poison_seen_and_watermark_rows(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A non-string seen/watermark value (a drifted external DB) is skipped, not
    fatal. parse_datetime RAISES TypeError on a non-str, so without the guard one
    such row would fail the whole load and brick setup forever — the same class of
    bug the daily_max salvage fixed, for the other two tables."""
    import sqlite3
    from datetime import datetime, timezone

    from custom_components.vigil.persistence import FlushSet

    path = tmp_path / "poison_seen.db"
    url = f"sqlite:///{path}"
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    w = SQLAlchemyIntervalStore(hass, url)
    await w.async_load()
    await w.async_flush(
        FlushSet(
            buckets={("s.good", now.toordinal()): 1.0}, seen={"s.good": (now, now)}
        )
    )
    await w.async_close()

    # Inject BLOB values (returned as `bytes`, not `str`) into the timestamp
    # columns — the poison shape that makes parse_datetime raise rather than
    # return None (a bad *string* it already tolerates).
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO seen (entity_id, first, last) VALUES (?, ?, ?)",
            ("s.bad", sqlite3.Binary(b"\x00"), sqlite3.Binary(b"\x00")),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("watermark", sqlite3.Binary(b"\x00")),
        )
        conn.commit()
    finally:
        conn.close()

    r = SQLAlchemyIntervalStore(hass, url)
    state = await r.async_load()  # must NOT raise
    await r.async_close()
    assert state.first_seen["s.good"] == now  # good row survives
    assert "s.bad" not in state.first_seen  # poison seen row skipped
    assert "s.bad" not in state.last_seen
    assert state.watermark is None  # poison watermark skipped


async def test_write_after_close_is_a_noop_not_a_resurrection(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A store op after async_close must NOT rebuild the engine — for the external
    backend that would leak a fresh pool. It no-ops via the best-effort path."""
    from datetime import datetime, timezone

    from custom_components.vigil.persistence import FlushSet

    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    store = SQLAlchemyIntervalStore(hass, f"sqlite:///{tmp_path / 'closed.db'}")
    await store.async_load()
    await store.async_close()

    assert (
        await store.async_flush(FlushSet(buckets={("s", now.toordinal()): 1.0}))
    ) is False
    assert await store.async_save_state("k", {"a": 1}) is False
    assert await store.async_load_state("k") is None
    assert store._engine is None  # never resurrected
