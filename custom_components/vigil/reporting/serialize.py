# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Pure JSON serialization of a cycle's :class:`VigilData` payload.

Separated from the HTTP view so the transform has no HA-view dependency and is
trivially testable.
"""

from __future__ import annotations

from ..models import VigilData, VigilStateDict


def serialize_vigil_data(data: VigilData) -> VigilStateDict:
    """Produce a fully JSON-serializable view of a :class:`VigilData` payload.

    Returns a :class:`VigilStateDict` — the typed wire contract the frontend
    types are generated from, so mypy flags any key that drifts from it.
    """
    now = data["last_run"]
    return VigilStateDict(
        issues=[issue.as_dict(now) for issue in data["issues"]],
        integration_failures=[i.as_dict(now) for i in data["integration_failures"]],
        devices_offline=[i.as_dict(now) for i in data["devices_offline"]],
        stale_devices=[i.as_dict(now) for i in data["stale_devices"]],
        device_faults=[i.as_dict(now) for i in data["device_faults"]],
        app_issues=[i.as_dict(now) for i in data["app_issues"]],
        counts=data["counts"],
        integration_health=data["integration_health"],
        last_run=now.isoformat(),
        healthy=data["healthy"],
        startup_grace_active=data["startup_grace_active"],
    )
