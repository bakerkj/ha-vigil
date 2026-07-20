# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.vigil.models import (
    DowntimeRecord,
    IssueKind,
    VigilIssue,
    issue_key,
)


def test_downtime_record_coerces_naive_since_to_aware() -> None:
    """A persisted naive ``since`` (an external/legacy row — Vigil itself always
    writes aware) is coerced to UTC on validation so it can't crash later tz-aware
    arithmetic (aware - naive raises TypeError); an already-aware value is left as
    is, not clobbered."""
    naive = DowntimeRecord.model_validate({"since": "2026-07-01T12:00:00"})
    assert naive.since.tzinfo is not None
    assert naive.since.utcoffset() == timedelta(0)  # assumed UTC

    aware = DowntimeRecord.model_validate({"since": "2026-07-01T12:00:00+05:00"})
    assert aware.since.utcoffset() == timedelta(hours=5)  # preserved, not clobbered


def _issue(
    kind: IssueKind,
    *,
    name: str = "X",
    source: str = "",
    device_id: str | None = None,
    entity_id: str | None = None,
    config_entry_id: str | None = None,
) -> VigilIssue:
    return VigilIssue(
        kind=kind,
        name=name,
        integration="I",
        detail="d",
        source=source,
        device_id=device_id,
        entity_id=entity_id,
        config_entry_id=config_entry_id,
    )


@pytest.mark.parametrize(
    ("kind", "kwargs", "expected"),
    [
        # App: slug from source, else the name.
        (IssueKind.APP_FAILED, {"name": "Rclone", "source": "rclone"}, "app:rclone"),
        (IssueKind.APP_FAILED, {"name": "Rclone"}, "app:Rclone"),
        # DEVICE_FAULT is per-faulted-entity: entity_id over device_id.
        (
            IssueKind.DEVICE_FAULT,
            {"entity_id": "sensor.x", "device_id": "dev1"},
            "fault:sensor.x",
        ),
        # A device issue prefers device_id over config_entry_id.
        (
            IssueKind.DEVICE_OFFLINE_CONFIRMED,
            {"device_id": "dev1", "config_entry_id": "e1"},
            "dev1",
        ),
        # No device: fall back to config entry, then the name.
        (IssueKind.INTEGRATION_FAILURE, {"config_entry_id": "e1"}, "e1"),
        (IssueKind.INTEGRATION_FAILURE, {"name": "Nameless"}, "Nameless"),
    ],
)
def test_issue_key(kind: IssueKind, kwargs: dict[str, str], expected: str) -> None:
    assert issue_key(_issue(kind, **kwargs)) == expected


def test_issue_key_app_failed_and_unstable_share_one_identity() -> None:
    # The same app flapping between FAILED and UNSTABLE must stay "one issue", so
    # an acknowledgement survives the transition instead of re-nagging.
    failed = _issue(IssueKind.APP_FAILED, source="rclone")
    unstable = _issue(IssueKind.APP_UNSTABLE, source="rclone")
    assert issue_key(failed) == issue_key(unstable) == "app:rclone"
