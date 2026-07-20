# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The per-cycle input snapshot handed to the detection pipeline.

The coordinator assembles one :class:`CycleContext` per refresh. Field bindings
are frozen, but the mutable collaborators it references (``downtime``,
``fault_state``) are still updated in place by Engines 2 and 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from homeassistant.core import HomeAssistant

    from .detection.engines.watch_config import FaultState, VigilConfigStore
    from .learning.interval_learner import IntervalLearner
    from .models import (
        AppHealthRecord,
        AppInfo,
        DeviceTuple,
        DowntimeRecord,
        ExclusionConfig,
    )


@dataclass(frozen=True)
class CycleContext:
    """Per-cycle inputs for the detection pipeline.

    ``frozen=True`` freezes the field *bindings* only. The referenced cross-cycle
    stores (``downtime``, ``fault_state``, ``app_health``) are deliberately
    mutable and are updated in place by their engines during the cycle.
    """

    hass: HomeAssistant
    now: datetime
    exclusions: ExclusionConfig
    staleness_exclusions: ExclusionConfig
    startup_grace_active: bool
    # Layer 2 output — per-device tuples with recorder downtime already seeded.
    tuples: list[DeviceTuple]
    # Cross-cycle per-device offline records; Engine 2 reads and GCs this in place.
    downtime: dict[str, DowntimeRecord]
    boot_time: datetime | None
    learner: IntervalLearner
    # Engine-4 per-rule debounce state; detect_watch_issues mutates this in place.
    fault_state: dict[str, FaultState]
    config_store: VigilConfigStore
    grace_period: timedelta
    battery_multiplier: float
    staleness_multiplier: float
    # Engine-5 Supervisor app snapshot: empty on non-Supervised installs, None
    # when the Supervisor read failed this cycle (Engine 5 is then skipped).
    apps: list[AppInfo] | None
    # Engine-5 per-app health/flap state; detect_app_issues mutates in place.
    app_health: dict[str, AppHealthRecord]
