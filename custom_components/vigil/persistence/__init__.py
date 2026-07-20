# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Interval-store persistence (Engine 3).

Public surface for the rest of the integration: the contract + shared types + the
store builder from :mod:`.interval_store`. The concrete backend
(:mod:`.sqlalchemy_backend`) is imported lazily by :func:`create_interval_store`
and not re-exported here.
"""

from __future__ import annotations

from .interval_store import (
    FlushSet,
    IntervalStoreError,
    IntervalStoreProtocol,
    LoadedState,
    create_interval_store,
)

__all__ = [
    "FlushSet",
    "IntervalStoreError",
    "IntervalStoreProtocol",
    "LoadedState",
    "create_interval_store",
]
