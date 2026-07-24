# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""A tiny persisted-value repository backed by the shared interval store.

Owns one in-memory value persisted as a JSON blob in the interval-store backend's
key-value ``state`` table — so fault/ack/downtime state lands in the SAME place as
the learned intervals (a local SQLite file or the configured external DB), not a
separate HA ``.storage`` file. A subclass supplies only its (de)serialization.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from homeassistant.core import HomeAssistant
from pydantic import BaseModel, ValidationError

_UNSET: Any = object()


def dump_model_map(models: Mapping[str, BaseModel]) -> dict[str, Any]:
    """Serialize a ``dict[str, BaseModel]`` to JSON-able rows for persistence."""
    return {key: model.model_dump(mode="json") for key, model in models.items()}


def load_model_map[M: BaseModel](
    model: type[M], data: Any, *, keep: Callable[[M], bool] | None = None
) -> dict[str, M]:
    """Rebuild a ``dict[str, model]`` from persisted JSON, skipping any row that
    fails validation — and, if ``keep`` is given, any validated row it rejects."""
    result: dict[str, M] = {}
    if not isinstance(data, dict):
        return result
    for key, raw in data.items():
        try:
            parsed = model.model_validate(raw)
        except ValidationError:
            continue
        if keep is None or keep(parsed):
            result[str(key)] = parsed
    return result


class StateStore(Protocol):
    """The key-value slice of the interval-store backend a repo persists through."""

    async def async_load_state(self, key: str) -> Any | None: ...

    async def async_save_state(self, key: str, value: Any) -> bool: ...


class StoreRepo[T]:
    """A single in-memory value persisted as a JSON blob under ``key``.

    ``deserialize`` MUST tolerate a missing/corrupt payload (return the empty
    value) so a corrupt store can never raise out of setup.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        store: StateStore,
        *,
        key: str,
        initial: T,
        serialize: Callable[[T], Any],
        deserialize: Callable[[Any], T],
    ) -> None:
        self._hass = hass
        self._store = store
        self._key = key
        self._value: T = initial
        self._serialize = serialize
        self._deserialize = deserialize
        self._last_saved: Any = _UNSET

    @property
    def state(self) -> T:
        """The live in-memory value the detection pass reads and mutates in place."""
        return self._value

    async def async_load(self) -> None:
        """Rehydrate the value from the store (called at setup)."""
        self._value = self._deserialize(await self._store.async_load_state(self._key))
        self._last_saved = self._serialize(self._value)

    def persist(self) -> None:
        """Save in the background, but only when the value actually changed (it
        changes at most once per cycle, and most cycles are no-ops).

        Delegates the write to :meth:`async_persist_now`, whose ``_last_saved``
        advances only once the write is CONFIRMED — so a swallowed write failure
        leaves it stale and the next changed cycle retries. A stable,
        non-re-derivable value (a floored ``since``, an acknowledgement) can't be
        silently lost to one transient DB blip.
        """
        if self._serialize(self._value) == self._last_saved:
            return
        self._hass.async_create_background_task(
            self.async_persist_now(), f"vigil-persist-{self._key}"
        )

    async def async_persist_now(self) -> None:
        """Write immediately (on unload / after a user action)."""
        payload = self._serialize(self._value)
        if await self._store.async_save_state(self._key, payload):
            self._last_saved = payload
