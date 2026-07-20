# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vigil import coordinator as coordinator_module
from custom_components.vigil.const import (
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_ENABLE_APP_MONITORING,
    CONF_ENABLE_NOTIFICATION,
    CONF_EXCLUDED_APPS,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_RECORDER_LOOKBACK_DAYS,
    CONF_STARTUP_IGNORE_SECONDS,
    DEFAULT_ENABLE_NOTIFICATION,
    MAX_RECORDER_LOOKBACK_DAYS,
    NOTIFICATION_ID,
    RECORDER_LOOKBACK_DAYS,
    STATE_ACK_KEY,
)
from custom_components.vigil.detection.engines.watch_config import FaultState
from custom_components.vigil.detection.inputs import build_device_tuples
from custom_components.vigil.history.recorder import select_downtime
from custom_components.vigil.models import (
    AppHealthRecord,
    AppInfo,
    ConnectivityState,
    FaultPhase,
    IssueKind,
    VigilIssue,
    issue_key,
)
from tests.helpers import (
    NO_EXCLUSIONS,
    _entry,
    _failed_entry,
    _make_coordinator,
    _offline_device,
)


def _patch_app_snapshot(monkeypatch: pytest.MonkeyPatch, *apps: AppInfo) -> None:
    """Patch the coordinator's Supervisor app snapshot to return ``apps`` each
    cycle — the static-snapshot setup the Engine-5 tests share."""

    async def _snapshot(_h: object) -> list[AppInfo]:
        return list(apps)

    monkeypatch.setattr(coordinator_module, "async_app_snapshot", _snapshot)


class _FakeRecorder:
    def __init__(self, keep_days: int) -> None:
        self.keep_days = keep_days


def _boom(_h: object) -> object:
    raise RuntimeError("recorder not ready")


@pytest.mark.parametrize(
    ("options", "get_instance", "expected"),
    [
        # Default (option 0 = auto) → the recorder's own purge_keep_days, read live.
        (None, lambda _h: _FakeRecorder(30), 30),
        # An explicit option wins over the recorder's retention.
        ({CONF_RECORDER_LOOKBACK_DAYS: 14}, lambda _h: _FakeRecorder(30), 14),
        # A recorder retaining absurdly long is clamped to the supported maximum.
        (None, lambda _h: _FakeRecorder(3650), MAX_RECORDER_LOOKBACK_DAYS),
        # If the recorder can't be read, fall back to the static default, not crash.
        (None, _boom, RECORDER_LOOKBACK_DAYS),
    ],
    ids=[
        "auto_matches_recorder_keep_days",
        "explicit_option_overrides_recorder",
        "auto_clamps_to_max",
        "falls_back_when_recorder_unreadable",
    ],
)
async def test_effective_lookback(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object] | None,
    get_instance: object,
    expected: int,
) -> None:
    """``_effective_lookback_days`` resolves the option/recorder/fallback rules."""
    coordinator = await _make_coordinator(hass, options=options)
    monkeypatch.setattr(coordinator_module, "get_instance", get_instance)
    assert coordinator._effective_lookback_days() == expected


async def test_detects_config_entry_failure(hass: HomeAssistant) -> None:
    """A non-LOADED config entry surfaces as an integration failure."""
    broken = _failed_entry(hass)

    coordinator = await _make_coordinator(hass)
    data = await coordinator._async_update_data()

    assert data["counts"]["integration_failures"] >= 1
    assert any(
        i.config_entry_id == broken.entry_id for i in data["integration_failures"]
    )
    assert data["healthy"] is False
    # The notification is created in place under the fixed id.
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert NOTIFICATION_ID in notifications


async def test_notification_disabled_suppresses_notification(
    hass: HomeAssistant,
) -> None:
    """With CONF_ENABLE_NOTIFICATION False, no persistent notification is made."""
    _failed_entry(hass)

    coordinator = await _make_coordinator(
        hass, options={CONF_ENABLE_NOTIFICATION: False}
    )
    data = await coordinator._async_update_data()

    # Issue still detected (panel/sensors unaffected) — just no notification.
    assert data["counts"]["integration_failures"] >= 1
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert NOTIFICATION_ID not in notifications


def _notif_exists(hass: HomeAssistant) -> bool:
    return (
        NOTIFICATION_ID
        in persistent_notification._async_get_or_create_notifications(hass)
    )


async def test_notification_off_by_default(hass: HomeAssistant) -> None:
    """Out of the box the persistent notification is OFF: a failing integration is
    still detected (sensors/card), but no notification is raised until the user
    opts in. Pins the production default the other notifier tests override."""
    assert DEFAULT_ENABLE_NOTIFICATION is False
    _failed_entry(hass)
    # Use the production default explicitly (the helper otherwise enables it).
    coordinator = await _make_coordinator(
        hass, options={CONF_ENABLE_NOTIFICATION: DEFAULT_ENABLE_NOTIFICATION}
    )

    data = await coordinator._async_update_data()
    assert data["counts"]["integration_failures"] >= 1  # detected
    assert not _notif_exists(hass)  # but silent by default


async def test_dismissed_alert_not_reraised_until_issue_returns(
    hass: HomeAssistant,
) -> None:
    """Dismissing the notification acknowledges its issues; it isn't re-raised
    while they persist, but a cleared-and-returned issue alerts again."""
    broken = _failed_entry(hass)
    coordinator = await _make_coordinator(hass)

    await coordinator._async_update_data()
    assert _notif_exists(hass)  # first detection raises it

    # User dismisses → next cycle acknowledges and does NOT re-raise.
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
    await coordinator._async_update_data()
    assert not _notif_exists(hass)
    await coordinator._async_update_data()  # still the same issue → still quiet
    assert not _notif_exists(hass)

    # Issue clears, then returns → it's a new occurrence → re-raised.
    broken.mock_state(hass, ConfigEntryState.LOADED)
    await coordinator._async_update_data()
    broken.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await coordinator._async_update_data()
    assert _notif_exists(hass)


async def test_clear_acknowledgements_reraises(hass: HomeAssistant) -> None:
    """The clear-acknowledgements action forgets dismissals so issues re-surface."""
    _failed_entry(hass)
    coordinator = await _make_coordinator(hass)

    await coordinator._async_update_data()
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
    await coordinator._async_update_data()
    assert not _notif_exists(hass)  # acknowledged

    await coordinator.async_clear_acknowledgements()  # clears + refreshes now
    assert coordinator._acknowledged == set()
    assert _notif_exists(hass)  # re-raised immediately


async def test_persisted_acknowledgement_survives_restart(
    hass: HomeAssistant,
) -> None:
    """An acknowledgement saved to disk is honored after a restart — the alert is
    not re-raised for an issue the user already dismissed."""
    broken = _failed_entry(hass)

    coordinator = await _make_coordinator(hass)
    # Pre-seed the shared store as if a prior session had acknowledged this failure.
    await coordinator.learner.store.async_save_state(
        STATE_ACK_KEY, {"acknowledged": [broken.entry_id]}
    )
    await coordinator.async_load_state()
    assert broken.entry_id in coordinator._acknowledged

    await coordinator._async_update_data()
    assert not _notif_exists(hass)  # honored across "restart" → no re-nag


async def test_fault_state_persists_to_db_across_restart(hass: HomeAssistant) -> None:
    """Engine-4 fault state round-trips through the shared store (the DB), not an HA
    .storage file: a new coordinator on the same store restores the fault with its
    original since."""
    since = dt_util.utcnow() - timedelta(hours=3)
    c1 = await _make_coordinator(hass)
    c1._faults.state["binary_sensor.node_component_warning"] = FaultState(
        phase=FaultPhase.ACTIVE,
        streak_since=since,
        since=since,
        detail="Failed: x",
        source="r",
        domain="esphome",
        clear_seconds=900,
    )
    await c1._faults.async_persist_now()

    # A fresh coordinator (as after a restart) reads the fault back from the store.
    c2 = await _make_coordinator(hass)
    await c2.async_load_state()
    fs = c2._faults.state
    assert "binary_sensor.node_component_warning" in fs
    assert fs["binary_sensor.node_component_warning"].since == since
    assert fs["binary_sensor.node_component_warning"].phase == "active"


async def test_app_issue_surfaces_and_health_persists(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine 5 wired end-to-end: a failing app from the (monkeypatched)
    Supervisor snapshot surfaces in the cycle output, is counted, and its health
    state round-trips the shared store across a restart."""
    _patch_app_snapshot(
        monkeypatch, AppInfo(slug="rclone", name="Rclone", state="error", boot="")
    )

    c1 = await _make_coordinator(hass)  # HA running → not in startup grace
    data = await c1._async_update_data()
    assert [i.source for i in data["app_issues"]] == ["rclone"]
    assert data["app_issues"][0].kind is IssueKind.APP_FAILED
    assert data["counts"]["app_issues"] == 1
    await c1._app_health.async_persist_now()

    # A fresh coordinator on the same store restores the app health.
    c2 = await _make_coordinator(hass)
    await c2.async_load_state()
    assert c2._app_health.state["rclone"].last_state == "error"


async def test_app_monitoring_toggle_off_skips_snapshot(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With app monitoring disabled, the snapshot fetch is skipped entirely and
    no app issue surfaces even for a failing app."""
    called: list[bool] = []

    async def _snapshot(_h: object) -> list[AppInfo]:
        called.append(True)
        return [AppInfo(slug="rclone", name="Rclone", state="error", boot="")]

    monkeypatch.setattr(coordinator_module, "async_app_snapshot", _snapshot)
    coordinator = await _make_coordinator(
        hass, options={CONF_ENABLE_APP_MONITORING: False}
    )
    data = await coordinator._async_update_data()
    assert data["app_issues"] == []
    assert called == []  # never fetched when disabled


async def test_app_exclusion_flows_from_options(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONF_EXCLUDED_APPS → ExclusionConfig.apps → Engine 5: an excluded slug
    yields no app issue."""
    _patch_app_snapshot(
        monkeypatch, AppInfo(slug="rclone", name="Rclone", state="error", boot="")
    )
    coordinator = await _make_coordinator(
        hass, options={CONF_EXCLUDED_APPS: ["rclone"]}
    )
    data = await coordinator._async_update_data()
    assert data["app_issues"] == []


async def test_supervisor_read_failure_preserves_app_health(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None snapshot (Supervisor read failed) must NOT wipe cross-cycle
    app-health state — a transient blip can't reset flap counts / failure since."""
    since = dt_util.utcnow() - timedelta(hours=3)

    async def _blip(_h: object) -> None:
        return None  # Supervisor read failed this cycle.

    monkeypatch.setattr(coordinator_module, "async_app_snapshot", _blip)
    coordinator = await _make_coordinator(hass)
    coordinator._app_health.state["rclone"] = AppHealthRecord(
        last_state="error", flaps=[since], since=since
    )

    data = await coordinator._async_update_data()

    # Engine 5 is skipped this cycle (no issue), and the record survives intact.
    assert data["app_issues"] == []
    rec = coordinator._app_health.state["rclone"]
    assert rec.since == since
    assert rec.flaps == [since]


async def test_dismissed_app_issue_acknowledged_until_it_clears(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app failure runs the acknowledge cycle like any other issue: dismissing
    the notification acknowledges it (keyed ``app:<slug>``), it isn't re-raised
    while it persists, and a cleared-then-returned failure alerts again."""
    app_state = {"value": "error"}

    # Kept inline (not _patch_app_snapshot): the returned state mutates per cycle.
    async def _snapshot(_h: object) -> list[AppInfo]:
        return [
            AppInfo(slug="rclone", name="Rclone", state=app_state["value"], boot="")
        ]

    monkeypatch.setattr(coordinator_module, "async_app_snapshot", _snapshot)
    coordinator = await _make_coordinator(hass)

    await coordinator._async_update_data()
    assert _notif_exists(hass)  # the failing app raises the notification

    # User dismisses → the app issue is acknowledged and does NOT re-raise.
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
    await coordinator._async_update_data()
    assert not _notif_exists(hass)
    assert "app:rclone" in coordinator._acknowledged  # keyed by slug, not device
    await coordinator._async_update_data()  # still failing → still quiet
    assert not _notif_exists(hass)

    # App recovers, then fails again → a new occurrence → re-raised.
    app_state["value"] = "started"
    await coordinator._async_update_data()
    app_state["value"] = "error"
    await coordinator._async_update_data()
    assert _notif_exists(hass)


def test_issue_key_stable_across_offline_kind_flip() -> None:
    """A device flapping confirmed<->no-signal keeps ONE acknowledge identity —
    issue_key is the target, not the kind — so it doesn't re-alert on each flip."""
    confirmed = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_CONFIRMED,
        name="D",
        integration="x",
        detail="",
        device_id="dev1",
    )
    no_signal = VigilIssue(
        kind=IssueKind.DEVICE_OFFLINE_NO_SIGNAL,
        name="D",
        integration="x",
        detail="",
        device_id="dev1",
    )
    assert issue_key(confirmed) == issue_key(no_signal) == "dev1"


async def test_acknowledgement_survives_startup_grace(hass: HomeAssistant) -> None:
    """A startup-grace cycle suppresses issues but must NOT drop acknowledgements —
    a suppressed issue isn't 'cleared', so it isn't re-nagged after grace lifts."""
    _failed_entry(hass)
    coordinator = await _make_coordinator(hass)  # not in grace (HA running)

    await coordinator._async_update_data()
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
    await coordinator._async_update_data()
    assert not _notif_exists(hass)  # acknowledged

    # Force one startup-grace cycle (issues suppressed).
    coordinator._ha_started = False
    coordinator._setup_time = dt_util.utcnow()
    data = await coordinator._async_update_data()
    assert data["startup_grace_active"] is True

    # Grace lifts; the issue persisted throughout → still acknowledged.
    coordinator._ha_started = True
    await coordinator._async_update_data()
    assert not _notif_exists(hass)


async def test_new_issue_reraises_while_others_stay_acknowledged(
    hass: HomeAssistant,
) -> None:
    """A genuinely new issue re-raises the notification even while other issues
    remain acknowledged (the multi-issue set-algebra branch)."""
    a = _failed_entry(hass, "A")
    b = _failed_entry(hass, "B")
    coordinator = await _make_coordinator(hass)

    await coordinator._async_update_data()
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
    await coordinator._async_update_data()
    assert not _notif_exists(hass)  # both acknowledged

    # A third failure appears → re-raised, and a/b are still acknowledged.
    c = _failed_entry(hass, "C")
    await coordinator._async_update_data()
    assert _notif_exists(hass)
    assert a.entry_id in coordinator._acknowledged
    assert b.entry_id in coordinator._acknowledged
    assert c.entry_id not in coordinator._acknowledged


async def test_lingering_notification_dismissed_when_all_acknowledged(
    hass: HomeAssistant,
) -> None:
    """A shown notification whose unacknowledged issues drain to empty — while an
    acknowledged issue is still active — is taken down, not left displaying the
    now-resolved content until the last issue clears."""
    a = _failed_entry(hass, "A")
    coordinator = await _make_coordinator(hass)

    await coordinator._async_update_data()  # raised showing A
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)  # user acknowledges A
    await coordinator._async_update_data()
    assert not _notif_exists(hass)

    # B appears → re-raised showing A + B.
    b = _failed_entry(hass, "B")
    await coordinator._async_update_data()
    assert _notif_exists(hass)

    # B resolves; A remains active but is acknowledged → the still-shown
    # notification is dismissed rather than left displaying the resolved B.
    b.mock_state(hass, ConfigEntryState.LOADED)
    await coordinator._async_update_data()
    assert not _notif_exists(hass)
    assert a.entry_id in coordinator._acknowledged


async def test_autoheal_dismiss_is_not_a_user_acknowledgement(
    hass: HomeAssistant,
) -> None:
    """When Vigil itself dismisses the notification (the issue healed on its own),
    that REMOVED event must NOT be read as a user dismissal — otherwise the issue
    would be silently acknowledged and wouldn't re-alert when it returns."""
    broken = _failed_entry(hass)
    coordinator = await _make_coordinator(hass)

    await coordinator._async_update_data()
    assert _notif_exists(hass)  # shown, NOT user-dismissed

    # Issue heals → Vigil auto-dismisses its own notification (self-initiated).
    broken.mock_state(hass, ConfigEntryState.LOADED)
    await coordinator._async_update_data()
    assert not _notif_exists(hass)
    assert broken.entry_id not in coordinator._acknowledged  # not acknowledged

    # Issue returns → it re-alerts (would stay silent if the auto-dismiss had been
    # mistaken for a user acknowledgement).
    broken.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await coordinator._async_update_data()
    assert _notif_exists(hass)


async def test_disabled_notification_prunes_cleared_ack(hass: HomeAssistant) -> None:
    """An acknowledged issue that clears while notifications are disabled is
    un-acknowledged, so it re-alerts when it returns after re-enabling — the prune
    runs even on the notifications-disabled path."""
    broken = _failed_entry(hass)
    coordinator = await _make_coordinator(hass)

    await coordinator._async_update_data()
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)  # acknowledge
    await coordinator._async_update_data()
    assert broken.entry_id in coordinator._acknowledged

    # Disable notifications, then the issue clears — the ack must still be pruned.
    coordinator._options = {**coordinator._options, CONF_ENABLE_NOTIFICATION: False}
    broken.mock_state(hass, ConfigEntryState.LOADED)
    await coordinator._async_update_data()
    assert broken.entry_id not in coordinator._acknowledged

    # Re-enable; the issue returns → re-alerts (not muted by a stale acknowledgement).
    coordinator._options = {**coordinator._options, CONF_ENABLE_NOTIFICATION: True}
    broken.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await coordinator._async_update_data()
    assert _notif_exists(hass)


async def test_healthy_when_all_loaded(hass: HomeAssistant) -> None:
    """No failing entries and no devices means a clean, healthy cycle."""
    coordinator = await _make_coordinator(hass)
    data = await coordinator._async_update_data()

    assert data["counts"]["total"] == 0
    assert data["healthy"] is True
    assert data["integration_failures"] == []


async def test_startup_grace_suppresses_issues(hass: HomeAssistant) -> None:
    """During the startup grace window every issue is suppressed."""
    broken = MockConfigEntry(domain="demo", title="Demo")
    broken.add_to_hass(hass)
    broken.mock_state(hass, ConfigEntryState.SETUP_ERROR)

    coordinator = await _make_coordinator(
        hass, options={CONF_STARTUP_IGNORE_SECONDS: 300}
    )
    # Force the startup window: pretend HA has not finished starting and Vigil
    # was just set up.
    coordinator._ha_started = False
    coordinator._setup_time = dt_util.utcnow()

    data = await coordinator._async_update_data()

    assert data["counts"]["total"] == 0
    # Detection is paused during grace — not a "healthy" claim.
    assert data["healthy"] is False
    assert data["startup_grace_active"] is True


@pytest.mark.parametrize(
    ("title", "devices", "expected"),
    [
        # No linked device → friendly integration name, never the entry title
        # (which can be a misleading account email).
        ("user@example.com", [], None),
        # Exactly one linked device → named by that device (the setup_retry title
        # has reverted to a generic default).
        ("HomeKit Device", [("tv", "Living Room TV")], "Living Room TV"),
        # More than one named device → can't pick one, fall back to the friendly
        # name (never a device name or the generic title).
        ("HomeKit Device", [("d1", "Thermostat"), ("d2", "Kitchen")], None),
    ],
)
async def test_integration_failure_naming(
    hass: HomeAssistant,
    title: str,
    devices: list[tuple[str, str]],
    expected: str | None,
) -> None:
    """An integration failure is named by its single linked device, else by the
    friendly integration label — never the raw (often misleading) entry title."""
    broken = _failed_entry(hass, title)
    reg = dr.async_get(hass)
    for ident, name in devices:
        reg.async_get_or_create(
            config_entry_id=broken.entry_id,
            identifiers={("demo", ident)},
            name=name,
        )

    coordinator = await _make_coordinator(hass)
    data = await coordinator._async_update_data()
    issue = data["integration_failures"][0]

    if expected is None:
        # Friendly integration label — never the raw title or any device name.
        assert issue.name == issue.integration
        assert issue.name != title
        assert issue.name not in {name for _, name in devices}
    else:
        assert issue.name == expected


async def test_offline_fires_after_grace_then_clears(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Engine 2 fires confirmed-offline only after grace, and clears on recovery.

    Also exercises the cross-cycle ``_downtime`` tracking and the rule
    that a connectivity signal entity does not count toward all_unavailable.
    """
    demo = _entry(hass, "demo", "Demo")
    device, data_id, _conn = _offline_device(hass, demo, "off1")

    coordinator = await _make_coordinator(
        hass,
        options={CONF_GRACE_PERIOD_MINUTES: 1, CONF_STARTUP_IGNORE_SECONDS: 0},
    )

    # Cycle 1: just observed offline — within grace, no issue yet.
    first = await coordinator._async_update_data()
    assert first["counts"]["devices_offline"] == 0
    assert device.id in coordinator._downtime

    # Advance past the 1-minute grace.
    freezer.tick(timedelta(minutes=2))
    second = await coordinator._async_update_data()
    assert second["counts"]["devices_offline"] == 1
    assert second["devices_offline"][0].kind is IssueKind.DEVICE_OFFLINE_CONFIRMED

    # Recovery: the data entity reports again — issue clears, tracking forgotten.
    hass.states.async_set(data_id, "21")
    third = await coordinator._async_update_data()
    assert third["counts"]["devices_offline"] == 0
    assert device.id not in coordinator._downtime


async def test_offline_devices_sorted_by_integration_then_name(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The offline list is ordered by integration, then device name."""
    demo = MockConfigEntry(domain="demo", title="Demo")
    demo.add_to_hass(hass)
    demo.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    # Create devices whose names are deliberately out of order.
    for name in ("Zulu", "Alpha", "Mike"):
        device = dev_reg.async_get_or_create(
            config_entry_id=demo.entry_id,
            identifiers={("demo", name)},
            name=name,
        )
        sensor = ent_reg.async_get_or_create(
            "sensor", "demo", f"{name}_s", device_id=device.id
        )
        hass.states.async_set(sensor.entity_id, "unavailable")

    coordinator = await _make_coordinator(
        hass,
        options={
            CONF_GRACE_PERIOD_MINUTES: 1,
            CONF_STARTUP_IGNORE_SECONDS: 0,
            CONF_BATTERY_GRACE_MULTIPLIER: 1.0,
        },
    )
    await coordinator._async_update_data()
    freezer.tick(timedelta(minutes=2))
    data = await coordinator._async_update_data()

    names = [i.name for i in data["devices_offline"]]
    assert names == ["Alpha", "Mike", "Zulu"]


async def test_battery_grace_multiplier_applied_to_battery_device(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A battery device's effective grace is grace * multiplier, end to end.

    GAP guard: every other coordinator test pins the multiplier to 1.0, so the
    ``effective_grace = grace * battery_multiplier`` branch (engine2) was never
    exercised through the coordinator. With grace=1m and multiplier=3.0 the
    battery device must NOT fire at 2 min (inside 3-min effective grace) and MUST
    fire at 4 min.
    """
    demo = MockConfigEntry(domain="demo", title="Demo")
    demo.add_to_hass(hass)
    demo.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=demo.entry_id, identifiers={("demo", "batt")}
    )
    # A battery sensor makes is_battery True; no connectivity signal -> UNKNOWN.
    batt = ent_reg.async_get_or_create(
        "sensor",
        "demo",
        "batt_level",
        device_id=device.id,
        original_device_class="battery",
    )
    hass.states.async_set(batt.entity_id, "unavailable")

    coordinator = await _make_coordinator(
        hass,
        options={
            CONF_GRACE_PERIOD_MINUTES: 1,
            CONF_STARTUP_IGNORE_SECONDS: 0,
            CONF_BATTERY_GRACE_MULTIPLIER: 3.0,
        },
    )
    # Confirm the device really is a battery device (the branch precondition).

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.is_battery is True

    # First cycle: observed offline now.
    first = await coordinator._async_update_data()
    assert not [i for i in first["devices_offline"] if i.device_id == device.id]

    # 2 min: past the 1-min base grace but INSIDE the 3-min effective grace.
    freezer.tick(timedelta(minutes=2))
    mid = await coordinator._async_update_data()
    assert not [i for i in mid["devices_offline"] if i.device_id == device.id], (
        "battery device must not fire inside grace * multiplier (3 min)"
    )

    # 4 min total: past the 3-min effective grace -> fires.
    freezer.tick(timedelta(minutes=2))
    late = await coordinator._async_update_data()
    assert [i for i in late["devices_offline"] if i.device_id == device.id], (
        "battery device must fire once grace * multiplier elapses"
    )


async def test_battery_grace_multiplier_not_applied_to_non_battery_down_device(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A non-battery device reporting DOWN uses the PLAIN grace, not grace *
    multiplier — so the multiplier never delays a confirmed-down device.

    Same multiplier (3.0) as the battery test, but here the device has a
    connectivity signal reporting DOWN and no battery sensor: at 2 min (past the
    1-min plain grace, inside the 3-min battery grace) it MUST already fire.
    """
    demo = _entry(hass, "demo", "Demo")
    device, _data, _conn = _offline_device(hass, demo, "wired")

    coordinator = await _make_coordinator(
        hass,
        options={
            CONF_GRACE_PERIOD_MINUTES: 1,
            CONF_STARTUP_IGNORE_SECONDS: 0,
            CONF_BATTERY_GRACE_MULTIPLIER: 3.0,
        },
    )

    t = {x.device_id: x for x in build_device_tuples(hass, NO_EXCLUSIONS)}[device.id]
    assert t.is_battery is False
    assert t.connectivity_state is ConnectivityState.DOWN

    first = await coordinator._async_update_data()
    assert not [i for i in first["devices_offline"] if i.device_id == device.id]

    # 2 min: past the 1-min plain grace. If the multiplier were (wrongly) applied
    # this would still be inside the 3-min battery grace and NOT fire.
    freezer.tick(timedelta(minutes=2))
    data = await coordinator._async_update_data()
    offline = [i for i in data["devices_offline"] if i.device_id == device.id]
    assert offline, "non-battery DOWN device must fire at plain grace (no multiplier)"
    assert offline[0].kind is IssueKind.DEVICE_OFFLINE_CONFIRMED


async def test_startup_grace_active_when_core_only_starting(
    hass: HomeAssistant,
) -> None:
    """Grace must engage when HA is still `starting` (not yet `running`).

    `hass.is_running` is True during `starting`, so the grace must not anchor on
    it — that would defeat the grace exactly during startup.
    """
    original = hass.state
    hass.set_state(CoreState.starting)
    try:
        coordinator = await _make_coordinator(
            hass, options={CONF_STARTUP_IGNORE_SECONDS: 300}
        )
        assert coordinator._ha_started is False
        data = await coordinator._async_update_data()
        assert data["startup_grace_active"] is True
    finally:
        hass.set_state(original)


async def test_recorder_seeds_true_offline_since(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine 2 downtime is seeded from the recorder for a new offline device."""
    hub = MockConfigEntry(domain="demo", title="Demo")
    hub.add_to_hass(hass)
    hub.mock_state(hass, ConfigEntryState.LOADED)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=hub.entry_id, identifiers={("demo", "rec")}
    )
    sensor = ent_reg.async_get_or_create("sensor", "demo", "rec_t", device_id=device.id)
    hass.states.async_set(sensor.entity_id, "unavailable")

    coordinator = await _make_coordinator(
        hass, options={CONF_STARTUP_IGNORE_SECONDS: 0}
    )
    # Pretend a recorder is present and returns a known true outage start.
    hass.config.components.add("recorder")
    true_since = dt_util.utcnow() - timedelta(hours=5)

    async def fake_last_good(
        hass: HomeAssistant,
        entity_to_device: dict[str, str],
        boot_time: datetime | None,
        now: datetime,
        lookback: timedelta,
    ) -> tuple[dict[str, datetime], set[str], set[str], bool]:
        return {device.id: true_since}, set(), set(), True

    monkeypatch.setattr(
        "custom_components.vigil.detection.engines.engine2_unavailability."
        "async_recorder_last_good",
        fake_last_good,
    )

    await coordinator._async_update_data()
    assert coordinator._downtime[device.id].since == true_since
    assert coordinator._downtime[device.id].recorder_resolved is True


_WINDOW = timedelta(seconds=300)


def test_select_downtime_floors_dead_all_window() -> None:
    """A device unavailable the whole window floors to the window edge; a device
    with a real recent reading keeps that reading."""
    start = dt_util.utcnow() - timedelta(days=7)
    good_ts = dt_util.utcnow() - timedelta(hours=3)
    history: dict[str, list[State | dict[str, object]]] = {
        "sensor.dead": [State("sensor.dead", "unavailable", last_changed=start)],
        "sensor.recent": [State("sensor.recent", "5", last_changed=good_ts)],
    }
    mapping = {"sensor.dead": "devA", "sensor.recent": "devB"}
    result, floored, _blind = select_downtime(
        history, mapping, None, [], _WINDOW, start
    )

    assert result["devB"] == good_ts
    assert result["devA"] == start
    assert floored == {"devA"}


def test_select_downtime_ignores_current_restart_writes() -> None:
    """States at/after boot_time (the current restart) are ignored; the true
    pre-restart reading wins, and a placeholder ('None') never counts as good."""
    start = dt_util.utcnow() - timedelta(days=7)
    boot = dt_util.utcnow() - timedelta(minutes=10)
    real_good = boot - timedelta(days=3)
    history: dict[str, list[State | dict[str, object]]] = {
        "sensor.lux": [
            State("sensor.lux", "5", last_changed=real_good),
            State("sensor.lux", "unavailable", last_changed=boot - timedelta(days=1)),
            State("sensor.lux", "unavailable", last_changed=boot),  # restart write
        ],
        "sensor.ver": [State("sensor.ver", "None", last_changed=boot)],
    }
    mapping = {"sensor.lux": "dev", "sensor.ver": "dev"}
    result, floored, _blind = select_downtime(
        history, mapping, boot, [], _WINDOW, start
    )

    assert result["dev"] == real_good
    assert "dev" not in floored


_DowntimeCase = tuple[
    dict[str, list[State | dict[str, object]]],
    dict[str, str],
    list[datetime],
    datetime,
    datetime,
    bool,
]


def _run_start_restore_mid_window() -> _DowntimeCase:
    """A restore that sits mid-window between unavailable states, at a run start."""
    start = dt_util.utcnow() - timedelta(days=7)
    base = dt_util.utcnow() - timedelta(days=4)
    restore = base + timedelta(hours=1)
    history: dict[str, list[State | dict[str, object]]] = {
        "sensor.plant": [
            State("sensor.plant", "unavailable", last_changed=start),
            State("sensor.plant", "unavailable", last_changed=base),
            State("sensor.plant", "42", last_changed=restore),  # restored at restart
            State(
                "sensor.plant",
                "unavailable",
                last_changed=restore + timedelta(minutes=1),
            ),
        ],
    }
    # Expect: floored to the window edge (start), not the restore instant.
    return history, {"sensor.plant": "dev"}, [restore], start, start, True


def _run_start_restore_at_window_start() -> _DowntimeCase:
    """The >window-dead boundary: a device that died BEFORE the window (its
    start-state is unavailable) but has a value restored at a restart must still
    floor — the restore is rejected as an artifact, not trusted."""
    start = dt_util.utcnow() - timedelta(days=7)
    restored = start + timedelta(seconds=30)
    history: dict[str, list[State | dict[str, object]]] = {
        "sensor.plant": [
            # window-start state: died before the window (include_start_time_state)
            State(
                "sensor.plant", "unavailable", last_changed=start - timedelta(days=2)
            ),
            State("sensor.plant", "42", last_changed=restored),  # restored at restart
            State(
                "sensor.plant",
                "unavailable",
                last_changed=restored + timedelta(minutes=1),
            ),
        ],
    }
    return history, {"sensor.plant": "dev"}, [start], start, start, True


def _genuine_reading_not_at_run_start() -> _DowntimeCase:
    """Contrast: a genuine reading NOT at a restart is trusted (no over-floor)."""
    start = dt_util.utcnow() - timedelta(days=6)
    real = start + timedelta(seconds=30)
    history: dict[str, list[State | dict[str, object]]] = {
        "sensor.plant": [
            State("sensor.plant", "42", last_changed=real),
            State(
                "sensor.plant", "unavailable", last_changed=real + timedelta(hours=2)
            ),
        ],
    }
    # Expect: trusted at the real reading, not floored.
    return history, {"sensor.plant": "dev"}, [], start, real, False


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_run_start_restore_mid_window, id="mid_window"),
        pytest.param(_run_start_restore_at_window_start, id="at_window_start"),
        pytest.param(_genuine_reading_not_at_run_start, id="genuine_reading_trusted"),
    ],
)
def test_select_downtime_run_start_restore_flooring(
    build: Callable[[], _DowntimeCase],
) -> None:
    """An isolated 'good' value at a recorder run-start instant is a restart
    artifact and floors to the window edge — whether the restore lands mid-window
    or exactly at the window start. A genuine reading NOT at a run start is
    trusted (no over-floor)."""
    history, mapping, run_starts, window_start, expected, expect_floored = build()
    result, floored, _blind = select_downtime(
        history, mapping, None, run_starts, _WINDOW, window_start
    )

    assert result["dev"] == expected
    assert ("dev" in floored) is expect_floored


def test_select_downtime_trusts_consecutive_reporting_at_run_starts() -> None:
    """A normally-reporting device is NOT floored even if every one of its report
    instants coincides with a restart instant (e.g. Frigate publishing many
    sensors in lockstep).

    A live device must not be floored to ">= window": only an ISOLATED good
    (preceded by a non-value state) at a restart instant is a restore artifact;
    consecutive good values are real and must be trusted.
    """
    start = dt_util.utcnow() - timedelta(days=7)
    last_good = dt_util.utcnow() - timedelta(minutes=5)
    seq: list[State | dict[str, object]] = [
        State("sensor.cam", "1", last_changed=start + timedelta(hours=1)),
        State("sensor.cam", "2", last_changed=dt_util.utcnow() - timedelta(hours=2)),
        State("sensor.cam", "3", last_changed=last_good),
        State(
            "sensor.cam",
            "unavailable",
            last_changed=dt_util.utcnow() - timedelta(minutes=4),
        ),
    ]
    history = {"sensor.cam": seq}
    mapping = {"sensor.cam": "cam"}
    # Worst case: every good report instant is also a recorded restart instant.
    run_starts = [s.last_changed for s in seq if isinstance(s, State)]
    result, floored, _blind = select_downtime(
        history, mapping, None, run_starts, _WINDOW, start
    )

    assert result["cam"] == last_good  # trusted — NOT floored
    assert "cam" not in floored


def test_select_downtime_floors_device_with_no_good_value() -> None:
    """A device whose recorder window holds no trustworthy reading is ALWAYS
    floored (never skipped) — the deliberate trade: never miss a dead device,
    even one that only ever showed unknown. But the floor is to the EVIDENCE edge
    (max(start, oldest row)), not blindly to the window: a device whose only row
    is an ``unknown`` 5 min ago has just 5 min of recorded history, so flooring to
    that row reads an honest "≥ 5m" rather than over-stating "≥ 7d"."""
    earliest = dt_util.utcnow() - timedelta(minutes=5)
    start = dt_util.utcnow() - timedelta(days=7)
    history: dict[str, list[State | dict[str, object]]] = {
        "sensor.new": [
            State(
                "sensor.new",
                "unknown",
                last_changed=earliest,
            )
        ],
    }
    mapping = {"sensor.new": "dev"}
    result, floored, _blind = select_downtime(
        history, mapping, None, [], _WINDOW, start
    )

    # Floored to the oldest evidence row (5 min), not the full window edge.
    assert result["dev"] == earliest
    assert "dev" in floored


def test_select_downtime_mixed_entities_one_with_rows_is_floored() -> None:
    """A device with TWO data entities where only ONE has recorder rows (and that
    one is dead the whole window) is treated as FLOORED, not recorder-blind.

    has_rows is per-device: any entity with rows means the recorder DOES see the
    device, so a no-good device is genuinely dead and floors. The blind path
    (which suppresses flooring) must only fire when NO entity has any rows at all —
    otherwise a device with one recorded-but-dead sensor and one excluded sensor
    would escape detection.
    """
    start = dt_util.utcnow() - timedelta(days=7)
    history: dict[str, list[State | dict[str, object]]] = {
        # Recorded, but dead the whole window (no good value).
        "sensor.recorded": [
            State("sensor.recorded", "unavailable", last_changed=start)
        ],
        # The other data entity has NO rows at all (recorder-excluded / never
        # recorded) — history.get returns [].
        "sensor.excluded": [],
    }
    mapping = {"sensor.recorded": "dev", "sensor.excluded": "dev"}
    result, floored, blind = select_downtime(history, mapping, None, [], _WINDOW, start)

    # The device has rows (via sensor.recorded) -> floored, NOT blind.
    assert result["dev"] == start
    assert "dev" in floored
    assert "dev" not in blind
