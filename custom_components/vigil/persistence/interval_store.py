# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The interval-store contract (Engine 3 persistence).

Defines the persistence interface the interval learner depends on plus the shared
value types. ``day`` is a proleptic-Gregorian ordinal (``datetime.toordinal()``).
The single backend lives beside this module and satisfies
:class:`IntervalStoreProtocol`; :func:`create_interval_store` builds it from the
options.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from homeassistant.core import HomeAssistant

from ..const import CONF_INTERVAL_STORE_URL, STORAGE_SQLITE_FILE


class IntervalStoreError(Exception):
    """Raised when the interval store cannot be read at load time.

    Setup converts this to ``ConfigEntryNotReady`` so HA retries; a load failure
    must never reach the learner as an empty/fresh state.
    """


@dataclass
class LoadedState:
    """Everything the learner needs to rehydrate in one shot."""

    daily_max: dict[str, dict[int, float]] = field(default_factory=dict)
    first_seen: dict[str, datetime] = field(default_factory=dict)
    last_seen: dict[str, datetime] = field(default_factory=dict)
    watermark: datetime | None = None


@dataclass
class FlushSet:
    """A batch of pending changes to persist (only what changed this cycle)."""

    # (entity_id, day) -> gap  to UPSERT into daily_max
    buckets: dict[tuple[str, int], float] = field(default_factory=dict)
    # entity_id -> (first, last)  to UPSERT into seen
    seen: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)
    # entity_ids fully removed (device gone) -> delete all their rows
    deleted: set[str] = field(default_factory=set)
    # drop every daily_max row with day < prune_before_day (None = no prune)
    prune_before_day: int | None = None
    # advance the recorder catch-up watermark (None = unchanged)
    watermark: datetime | None = None

    def is_empty(self) -> bool:
        return not (
            self.buckets
            or self.seen
            or self.deleted
            or self.prune_before_day is not None
            or self.watermark is not None
        )


def create_interval_store(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> IntervalStoreProtocol:
    """Build the interval store from the merged options.

    One SQLAlchemy backend drives both cases: a non-blank
    ``CONF_INTERVAL_STORE_URL`` (a SQLAlchemy URL) targets an external DB;
    otherwise the default local SQLite file at
    ``.storage/<STORAGE_SQLITE_FILE>`` via a ``sqlite:///…`` URL. SQLAlchemy is
    always present in the HA runtime, so the import stays local only to keep this
    module import-light.
    """
    from sqlalchemy.engine import URL

    from .sqlalchemy_backend import SQLAlchemyIntervalStore

    url = str(options.get(CONF_INTERVAL_STORE_URL, "") or "").strip()
    if not url:
        path = hass.config.path(".storage", STORAGE_SQLITE_FILE)
        url = URL.create("sqlite", database=path).render_as_string(hide_password=False)
    return SQLAlchemyIntervalStore(hass, url)


class IntervalStoreProtocol(Protocol):
    """The persistence contract the interval learner depends on."""

    async def async_load(self) -> LoadedState:
        """Read the full store into memory.

        Raises :class:`IntervalStoreError` if the store cannot be read, so a
        failure is never mistaken for an empty/fresh store.
        """
        ...

    async def async_flush(self, changes: FlushSet) -> bool:
        """Apply a batch of changes; return True iff it was persisted.

        A False return tells the learner to keep its dirty state and not advance
        the watermark, so the batch is retried next cycle.
        """
        ...

    async def async_close(self) -> None:
        """Release any held resources (e.g. a connection pool) on unload."""
        ...

    async def async_load_state(self, key: str) -> Any | None:
        """Read a JSON-able value from the shared key-value ``state`` table (None
        if absent/unreadable). Backs the fault/ack/downtime repositories so ALL
        Vigil state lands in the one configured store."""
        ...

    async def async_save_state(self, key: str, value: Any) -> bool:
        """Upsert a JSON-able value into the shared key-value ``state`` table.
        Returns True on a confirmed write, False on a swallowed failure."""
        ...
