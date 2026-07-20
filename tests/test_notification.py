# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.vigil.models import (
    IssueKind,
    VigilData,
    VigilIssue,
    humanize_duration,
)
from custom_components.vigil.reporting.notification import render_message


def _empty_data(now: datetime) -> VigilData:
    """A healthy payload (no issues)."""
    return {
        "issues": [],
        "integration_failures": [],
        "devices_offline": [],
        "stale_devices": [],
        "device_faults": [],
        "app_issues": [],
        "counts": {"total": 0},
        "integration_health": [],
        "last_run": now,
        "healthy": True,
        "startup_grace_active": False,
    }


def _full_data(now: datetime) -> VigilData:
    """A payload with one issue of every kind."""
    since = now - timedelta(hours=2, minutes=5)

    failure = VigilIssue(
        kind=IssueKind.INTEGRATION_FAILURE,
        name="Hue Bridge",
        integration="hue",
        detail="config entry failed to set up",
        since=since,
        source="setup_retry",
    )
    confirmed = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_CONFIRMED,
        name="Living Room Lamp",
        integration="hue",
        detail="router reports device down",
        since=now - timedelta(minutes=30),
        source="unifi",
    )
    no_signal = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_NO_SIGNAL,
        name="Garage Sensor",
        integration="zwave_js",
        detail="all entities unavailable",
        since=now - timedelta(days=1, hours=3),
        source="",
    )
    stale = VigilIssue(
        kind=IssueKind.SILENT_DEVICE,
        name="Attic Temp",
        integration="mqtt",
        detail="last update 45m ago, expected ~15m",
        since=now - timedelta(minutes=45),
        source="",
    )

    offline = [confirmed, no_signal]
    failures = [failure]
    stale_list = [stale]
    issues = [failure, confirmed, no_signal, stale]

    return {
        "issues": issues,
        "integration_failures": failures,
        "devices_offline": offline,
        "stale_devices": stale_list,
        "device_faults": [],
        "app_issues": [],
        "counts": {"total": len(issues)},
        "integration_health": [],
        "last_run": now,
        "healthy": False,
        "startup_grace_active": False,
    }


# --- humanize_duration -------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "unknown"),
        (0.0, "<1m"),
        (59.0, "<1m"),
        (60.0, "1m"),
        (90.0, "1m"),
        (59 * 60.0, "59m"),
        (60 * 60.0, "1h 0m"),
        (2 * 3600 + 5 * 60.0, "2h 5m"),
        (23 * 3600 + 59 * 60.0, "23h 59m"),
        (24 * 3600.0, "1d 0h"),
        (27 * 3600.0, "1d 3h"),
    ],
)
def test_humanize_duration(seconds: float | None, expected: str) -> None:
    assert humanize_duration(seconds) == expected


# --- render_message ----------------------------------------------------------


def test_render_message_header_only_when_empty() -> None:
    now = dt_util.utcnow()
    msg = render_message(_empty_data(now), now)
    assert msg == "⚠️ Vigil — 0 issue(s) detected"


def test_render_message_includes_all_sections() -> None:
    now = dt_util.utcnow()
    msg = render_message(_full_data(now), now)

    assert msg.startswith("⚠️ Vigil — 4 issue(s) detected")

    assert "INTEGRATION FAILURES (1)" in msg
    assert "• Hue Bridge — setup_retry since 2h 5m" in msg

    assert "DEVICES OFFLINE — network confirmed (1)" in msg
    assert "• Living Room Lamp — unifi DOWN 30m (hue)" in msg

    assert "DEVICES OFFLINE — no network signal (1)" in msg
    assert "• Garage Sensor — all entities unavailable 1d 3h (zwave_js)" in msg

    assert "SILENT DEVICES — network UP, data stale (1)" in msg
    assert "• Attic Temp — last update 45m ago, expected ~15m (mqtt)" in msg


def test_render_message_includes_device_faults_section() -> None:
    now = dt_util.utcnow()
    fault = VigilIssue(
        kind=IssueKind.DEVICE_FAULT,
        name="Attic Node",
        integration="ESPHome",
        detail="Component fault: warning",
        device_id="dev1",
        domain="esphome",
    )
    data = _empty_data(now)
    data["device_faults"] = [fault]
    data["issues"] = [fault]
    data["counts"]["total"] = 1
    data["healthy"] = False

    msg = render_message(data, now)
    assert "DEVICE FAULTS — watch rule triggered (1)" in msg
    assert "• Attic Node — Component fault: warning (ESPHome)" in msg


def test_render_message_includes_app_section() -> None:
    now = dt_util.utcnow()
    app = VigilIssue(
        kind=IssueKind.APP_FAILED,
        name="Rclone Backup",
        integration="App",
        detail="crashed (error state)",
        source="rclone",
    )
    data = _empty_data(now)
    data["app_issues"] = [app]
    data["issues"] = [app]
    data["counts"]["total"] = 1
    data["healthy"] = False

    msg = render_message(data, now)
    assert "APPS (1)" in msg
    # detail's parens are markdown-escaped by _md, so match the words.
    assert "• Rclone Backup — crashed" in msg


def test_render_message_excludes_empty_sections() -> None:
    now = dt_util.utcnow()
    data = _full_data(now)
    # Keep only the stale section populated.
    data["integration_failures"] = []
    data["devices_offline"] = []
    data["counts"]["total"] = len(data["stale_devices"])

    msg = render_message(data, now)

    assert "INTEGRATION FAILURES" not in msg
    assert "DEVICES OFFLINE — network confirmed" not in msg
    assert "DEVICES OFFLINE — no network signal" not in msg
    assert "SILENT DEVICES — network UP, data stale (1)" in msg


def test_render_message_splits_offline_by_kind() -> None:
    now = dt_util.utcnow()
    data = _full_data(now)
    # Only a confirmed-offline issue present.
    confirmed = next(
        i
        for i in data["devices_offline"]
        if i.kind == IssueKind.DEVICE_OFFLINE_CONFIRMED
    )
    data["devices_offline"] = [confirmed]
    data["counts"]["total"] = 1
    data["integration_failures"] = []
    data["stale_devices"] = []

    msg = render_message(data, now)
    assert "DEVICES OFFLINE — network confirmed (1)" in msg
    assert "DEVICES OFFLINE — no network signal" not in msg


def test_render_message_robust_when_since_none() -> None:
    now = dt_util.utcnow()
    issue = VigilIssue(
        kind=IssueKind.INTEGRATION_FAILURE,
        name="Mystery",
        integration="foo",
        detail="no timestamp",
        since=None,
        source="setup_error",
    )
    data = _empty_data(now)
    data["integration_failures"] = [issue]
    data["issues"] = [issue]
    data["counts"]["total"] = 1

    msg = render_message(data, now)
    # Integration failures have no timestamp, so the duration suffix is omitted.
    assert "• Mystery — setup_error" in msg
    assert "since unknown" not in msg


def test_render_message_escapes_markdown_injection_in_names() -> None:
    """A malicious device/integration name cannot inject a markdown link."""
    now = dt_util.utcnow()
    issue = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_NO_SIGNAL,
        name="[pwn](javascript:alert(1))",
        integration="evil`code`",
        detail="x",
        since=now - timedelta(minutes=5),
        source="none",
    )
    data = _empty_data(now)
    data["devices_offline"] = [issue]
    data["issues"] = [issue]
    data["counts"]["total"] = 1

    msg = render_message(data, now)
    # The link/code syntax is neutralized (brackets/parens/backticks escaped).
    assert "[pwn]" not in msg
    assert "(javascript:alert(1))" not in msg
    assert "`code`" not in msg
    assert "\\[pwn\\]" in msg


def test_render_message_keeps_underscores_in_names() -> None:
    """Cosmetic emphasis chars are left alone so domains render normally."""
    now = dt_util.utcnow()
    issue = VigilIssue(
        kind=IssueKind.SILENT_DEVICE,
        name="Attic Temp",
        integration="zwave_js",
        detail="stale",
        since=now,
        source="stale",
    )
    data = _empty_data(now)
    data["stale_devices"] = [issue]
    data["issues"] = [issue]
    data["counts"]["total"] = 1

    assert "(zwave_js)" in render_message(data, now)


def test_render_message_caps_section_with_overflow_pointer() -> None:
    """Sections are capped; the header keeps the full count and an overflow line points to the card."""
    now = dt_util.utcnow()
    stale = [
        VigilIssue(
            kind=IssueKind.SILENT_DEVICE,
            name=f"Sensor {i}",
            integration="mqtt",
            detail="stale",
            since=now,
            source="",
        )
        for i in range(25)
    ]
    data = _empty_data(now)
    data["stale_devices"] = stale
    data["issues"] = stale
    data["counts"] = {"total": 25}

    msg = render_message(data, now)
    assert "SILENT DEVICES — network UP, data stale (25)" in msg  # full count
    assert msg.count("• Sensor ") == 20  # capped
    assert "…and 5 more — see the Vigil card" in msg


def test_render_message_shows_lower_bound_prefix() -> None:
    """A lower-bound issue renders its duration with a ≥ prefix."""
    now = dt_util.utcnow()
    issue = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_NO_SIGNAL,
        name="Dead Plant",
        integration="xiaomi_ble",
        detail="all entities unavailable",
        since=now - timedelta(days=7),
        source="",
        since_is_lower_bound=True,
    )
    data = _empty_data(now)
    data["devices_offline"] = [issue]
    data["issues"] = [issue]
    data["counts"] = {"total": 1}

    msg = render_message(data, now)
    assert "≥ " in msg  # lower-bound duration prefix rendered
