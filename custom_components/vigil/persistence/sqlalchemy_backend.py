# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The SQLAlchemy-backed interval store — the single persistence backend.

Persists Vigil's learned intervals + the fault/ack/downtime state over a
Vigil-owned connection (separate from the recorder's engine and pool). It drives
BOTH the default local SQLite file (``sqlite:///…``) and an optional external DB
(a MySQL/MariaDB URL). Schema and statements use SQLAlchemy Core, so the column
types and UPSERT clause are generated per dialect (SQLite ``ON CONFLICT`` /
``REAL`` vs. MySQL/MariaDB ``ON DUPLICATE KEY`` / ``DOUBLE``). A local SQLite file
gets WAL + ``synchronous=NORMAL`` via a connect-time pragma hook and a fresh
connection per operation (``NullPool``); a remote DB keeps a pinged, recycled
pool. Persistence is best-effort: a DB blip is logged and skipped rather than
breaking a cycle.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    bindparam,
    create_engine,
    delete,
    event,
    select,
)
from sqlalchemy.dialects.mysql import DOUBLE
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.dml import Insert

from .interval_store import FlushSet, IntervalStoreError, LoadedState

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

_METADATA = MetaData()

# One schema for both dialects; SQLAlchemy renders the per-dialect types and
# auto-quotes the reserved word ``key``.
_DAILY_MAX = Table(
    "daily_max",
    _METADATA,
    Column("entity_id", String(255), primary_key=True),
    Column("day", Integer, primary_key=True),
    # DOUBLE on MySQL (matches the recorder's precision); REAL on SQLite.
    Column("gap", Float().with_variant(DOUBLE(), "mysql"), nullable=False),
)
_SEEN = Table(
    "seen",
    _METADATA,
    Column("entity_id", String(255), primary_key=True),
    Column("first", String(64), nullable=False),
    Column("last", String(64), nullable=False),
)


def _parse_dt(value: Any) -> datetime | None:
    """Parse a persisted ISO timestamp defensively.

    Tolerates a poison non-string row (NULL/int/BLOB) — ``parse_datetime`` RAISES
    ``TypeError`` on a non-str, so a single corrupt seen/watermark value would
    otherwise fail the whole load (→ ConfigEntryNotReady, retrying a permanent
    corruption forever). Also coerces a naive value to UTC so a legacy/external
    row can't crash later tz-aware arithmetic. Returns None on anything unparsable.
    """
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _kv_table(name: str) -> Table:
    """A ``key``→JSON ``value`` table (SQLAlchemy auto-quotes the reserved word)."""
    return Table(
        name,
        _METADATA,
        Column("key", String(64), primary_key=True),
        Column("value", Text, nullable=False),
    )


# ``meta`` holds the recorder watermark; ``state`` is the shared key-value table
# for the fault/ack/downtime repositories (JSON blobs), so all Vigil state lands
# in the one configured store.
_META = _kv_table("meta")
_STATE = _kv_table("state")


def _upsert(table: Table, dialect: str, cols: tuple[str, ...]) -> Insert:
    """INSERT ... UPSERT for ``table``, overwriting ``cols`` on a PK conflict."""
    if dialect == "sqlite":
        sq = sqlite_insert(table)
        return cast(
            Insert,
            sq.on_conflict_do_update(
                index_elements=list(table.primary_key.columns),
                set_={c: sq.excluded[c] for c in cols},
            ),
        )
    my = mysql_insert(table)
    return cast(Insert, my.on_duplicate_key_update(**{c: my.inserted[c] for c in cols}))


def _set_sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    """WAL + NORMAL on every new SQLite connection — the local-file tuning that
    makes the per-cycle flush cheap."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


class SQLAlchemyIntervalStore:
    """The SQLAlchemy-backed :class:`.interval_store.IntervalStoreProtocol`.

    See the module docstring for the dual-backend and per-dialect pooling design.
    The engine is created lazily and every operation runs in its own transaction
    (``engine.begin()``).
    """

    def __init__(self, hass: HomeAssistant, url: str) -> None:
        self._hass = hass
        self._url = url
        self._engine: Engine | None = None
        self._closed = False

    def _get_engine(self) -> Engine:
        if self._closed:
            # A late fire-and-forget write after unload must not silently
            # resurrect (and leak) a fresh engine/pool; fail so the best-effort
            # wrapper swallows it into a no-op instead.
            raise IntervalStoreError("interval store is closed")
        if self._engine is None:
            url = make_url(self._url)
            if url.get_backend_name() == "sqlite":
                # Ensure the file's directory exists (SQLAlchemy won't mkdir it),
                # then a fresh connection per op (NullPool) with the WAL pragmas.
                if url.database and url.database != ":memory:":
                    os.makedirs(
                        os.path.dirname(os.path.abspath(url.database)), exist_ok=True
                    )
                engine = create_engine(self._url, poolclass=NullPool, future=True)
                event.listen(engine, "connect", _set_sqlite_pragmas)
            else:
                engine = create_engine(
                    self._url, pool_pre_ping=True, pool_recycle=3600, future=True
                )
            # CREATE TABLE IF NOT EXISTS (checkfirst) — once per engine.
            _METADATA.create_all(engine)
            self._engine = engine
        return self._engine

    def _target(self) -> str:
        """The URL with any password redacted, for error/log messages.

        Never raises: it runs inside error handlers and the best-effort swallow
        path, so an unparsable URL (a plain ``ValueError`` from e.g. a bad port,
        which config_flow doesn't validate — not only ``SQLAlchemyError``) must
        degrade to a safe placeholder, never escape and defeat that contract."""
        try:
            return make_url(self._url).render_as_string(hide_password=True)
        except Exception:  # noqa: BLE001 - a redaction helper must never raise
            return "<unparsable url>"

    # --- async orchestration (offloads the sync SQL below to the executor) ---

    async def _best_effort(
        self,
        fn: Callable[..., _T],
        *args: Any,
        warn: str,
        warn_args: tuple[Any, ...],
        fail_value: _T,
    ) -> _T:
        """Run a sync store op on the executor; on any failure log ``warn`` (with
        ``warn_args`` then the redacted target) and return ``fail_value``.

        The shared best-effort path for the non-load ops: a DB blip is swallowed
        so it can't break a detection cycle or unload. ``async_load`` is
        deliberately NOT routed here — a load failure must raise so setup retries.
        """
        try:
            return await self._hass.async_add_executor_job(fn, *args)
        except Exception:  # noqa: BLE001 - best-effort; swallow and return sentinel
            _LOGGER.warning(warn, *warn_args, self._target(), exc_info=True)
            return fail_value

    async def async_load(self) -> LoadedState:
        try:
            return await self._hass.async_add_executor_job(self._load)
        except Exception as err:  # noqa: BLE001 - any load failure must surface
            # as IntervalStoreError so setup retries, never a silent empty state.
            raise IntervalStoreError(
                f"interval store load from {self._target()} failed"
            ) from err

    async def async_flush(self, changes: FlushSet) -> bool:
        if changes.is_empty():
            return True
        return await self._best_effort(
            self._flush,
            changes,
            warn="Vigil: interval store flush to %s failed; keeping batch for retry",
            warn_args=(),
            fail_value=False,
        )

    async def async_load_state(self, key: str) -> Any | None:
        """Read a JSON-able value from the shared ``state`` key-value table, or
        None if absent/unreadable. Best-effort: a failure returns None (the caller
        starts empty) rather than raising — this state is re-derivable, unlike the
        learned intervals."""
        return await self._best_effort(
            self._load_state,
            key,
            warn="Vigil: state load (%s) from %s failed; starting empty",
            warn_args=(key,),
            fail_value=None,
        )

    async def async_save_state(self, key: str, value: Any) -> bool:
        """Upsert a JSON-able value into the shared ``state`` key-value table.

        Best-effort — never raises, so it can't break a cycle or unload — but
        returns ``True`` only on a confirmed write and ``False`` on a swallowed
        failure, so the caller can retry a later cycle instead of assuming it
        saved."""
        return await self._best_effort(
            self._save_state,
            key,
            value,
            warn="Vigil: state save (%s) to %s failed",
            warn_args=(key,),
            fail_value=False,
        )

    # --- sync SQL (run inside the executor by the async methods above) -------

    def _load(self) -> LoadedState:
        engine = self._get_engine()
        state = LoadedState()
        with engine.begin() as conn:
            for row in conn.execute(
                select(_DAILY_MAX.c.entity_id, _DAILY_MAX.c.day, _DAILY_MAX.c.gap)
            ):
                try:
                    day, gap = int(row[1]), float(row[2])
                except TypeError, ValueError:
                    # Salvage per-row like the seen/watermark rows below: a single
                    # poison bucket (non-numeric day/gap from a corrupt or drifted
                    # external DB) must not fail the whole load — that would raise
                    # ConfigEntryNotReady and retry setup forever on a PERMANENT
                    # corruption, never self-healing. Skipping one entity-day of
                    # learned cadence self-heals within the horizon.
                    _LOGGER.warning(
                        "Vigil: skipping unparsable daily_max row for %r", row[0]
                    )
                    continue
                state.daily_max.setdefault(row[0], {})[day] = gap
            for row in conn.execute(
                select(_SEEN.c.entity_id, _SEEN.c.first, _SEEN.c.last)
            ):
                first = _parse_dt(row[1])
                last = _parse_dt(row[2])
                if first is not None:
                    state.first_seen[row[0]] = first
                if last is not None:
                    state.last_seen[row[0]] = last
            wm_row = conn.execute(
                select(_META.c.value).where(_META.c["key"] == "watermark")
            ).fetchone()
            if wm_row is not None:
                state.watermark = _parse_dt(wm_row[0])
        return state

    async def async_close(self) -> None:
        """Dispose the engine and its connection pool on unload. Idempotent.

        Marks the store closed first, so a late fire-and-forget write (a persist()
        task from the final pre-unload cycle landing after this) can't resurrect
        and leak a fresh engine/pool via ``_get_engine`` — it no-ops instead."""
        self._closed = True
        if self._engine is not None:
            engine, self._engine = self._engine, None
            await self._hass.async_add_executor_job(engine.dispose)

    def _flush(self, changes: FlushSet) -> bool:
        engine = self._get_engine()
        dialect = engine.dialect.name
        with engine.begin() as conn:
            if changes.deleted:
                params: list[dict[str, Any]] = [{"e": e} for e in changes.deleted]
                conn.execute(
                    delete(_DAILY_MAX).where(_DAILY_MAX.c.entity_id == bindparam("e")),
                    params,
                )
                conn.execute(
                    delete(_SEEN).where(_SEEN.c.entity_id == bindparam("e")), params
                )
            if changes.buckets:
                conn.execute(
                    _upsert(_DAILY_MAX, dialect, ("gap",)),
                    [
                        {"entity_id": e, "day": day, "gap": g}
                        for (e, day), g in changes.buckets.items()
                    ],
                )
            if changes.seen:
                conn.execute(
                    _upsert(_SEEN, dialect, ("first", "last")),
                    [
                        {
                            "entity_id": e,
                            "first": f.isoformat(),
                            "last": latest.isoformat(),
                        }
                        for e, (f, latest) in changes.seen.items()
                    ],
                )
            if changes.prune_before_day is not None:
                conn.execute(
                    delete(_DAILY_MAX).where(
                        _DAILY_MAX.c.day < changes.prune_before_day
                    )
                )
            if changes.watermark is not None:
                conn.execute(
                    _upsert(_META, dialect, ("value",)),
                    {"key": "watermark", "value": changes.watermark.isoformat()},
                )
        return True

    def _load_state(self, key: str) -> Any | None:
        engine = self._get_engine()
        with engine.begin() as conn:
            row = conn.execute(
                select(_STATE.c.value).where(_STATE.c["key"] == key)
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def _save_state(self, key: str, value: Any) -> bool:
        engine = self._get_engine()
        with engine.begin() as conn:
            conn.execute(
                _upsert(_STATE, engine.dialect.name, ("value",)),
                {"key": key, "value": json.dumps(value)},
            )
        return True
