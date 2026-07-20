# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The acknowledged-issues repository.

Owns the persisted set of issue keys the user has dismissed (Layer 5), persisted
in the shared interval store so the contract lives in one place.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..const import STATE_ACK_KEY
from ..storage import StateStore, StoreRepo


def _serialize_acks(acknowledged: set[str]) -> dict[str, list[str]]:
    return {"acknowledged": sorted(acknowledged)}


def _deserialize_acks(data: Any) -> set[str]:
    # Guard the on-disk shape: a corrupted store must start empty, not fail setup.
    if isinstance(data, dict) and isinstance(data.get("acknowledged"), list):
        return set(data["acknowledged"])
    return set()


class AckRepo(StoreRepo[set[str]]):
    """Persisted set of acknowledged issue keys (see :class:`..storage.StoreRepo`)."""

    def __init__(self, hass: HomeAssistant, store: StateStore) -> None:
        super().__init__(
            hass,
            store,
            key=STATE_ACK_KEY,
            initial=set(),
            serialize=_serialize_acks,
            deserialize=_deserialize_acks,
        )

    @property
    def acknowledged(self) -> set[str]:
        """The current acknowledged-key set (read-only view of the live set)."""
        return self._value

    def set(self, keys: set[str]) -> None:
        """Replace the acknowledged set (callers persist separately)."""
        self._value = keys
