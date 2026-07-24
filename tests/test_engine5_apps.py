# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vigil.detection.engines import engine5_apps as mod
from custom_components.vigil.detection.engines.engine5_apps import (
    _deserialize_app_health,
    _serialize_app_health,
    async_app_snapshot,
    detect_app_issues,
)
from custom_components.vigil.models import (
    AppHealthRecord,
    AppInfo,
    IssueKind,
    VigilIssue,
)

_History = dict[str, AppHealthRecord]

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _app(slug: str, state: str, boot: str = "", startup: str = "") -> AppInfo:
    return AppInfo(
        slug=slug,
        name=slug.replace("_", " ").title(),
        state=state,
        boot=boot,
        startup=startup,
    )


def _feed(
    states: list[str], *, threshold: int = 3, window_seconds: int = 3600, step: int = 60
) -> tuple[list[VigilIssue], _History]:
    """Drive one app through a sequence of observed states, one per cycle."""
    history: _History = {}
    issues: list[VigilIssue] = []
    t = NOW
    for state in states:
        issues = detect_app_issues(
            [_app("z", state)],
            history=history,
            now=t,
            threshold=threshold,
            window_seconds=window_seconds,
        )
        t += timedelta(seconds=step)
    return issues, history


def test_info_blip_on_failed_app_keeps_since_and_stays_flagged() -> None:
    """A per-app info-read blip returns boot="" for a stopped app. On an app that is
    ALREADY failing, that must NOT read as recovery: its `since` is preserved and the
    issue keeps surfacing, so a transient Supervisor blip can't drop the alert and
    then re-notify an acknowledged failure on the next good read."""
    history: _History = {}
    t0 = NOW
    # Cycle 1: stopped + boot=auto → failed, since stamped, issue raised.
    issues = detect_app_issues(
        [_app("foo", "stopped", boot="auto")], history=history, now=t0
    )
    assert [i.kind for i in issues] == [IssueKind.APP_FAILED]
    assert history["foo"].since == t0

    # Cycle 2: info read failed → boot="" (unknown). Must stay failed, since kept.
    t1 = t0 + timedelta(minutes=1)
    issues = detect_app_issues(
        [_app("foo", "stopped", boot="")], history=history, now=t1
    )
    assert [i.kind for i in issues] == [IssueKind.APP_FAILED]
    assert history["foo"].since == t0  # NOT reset to None (and not re-stamped to t1)
    assert issues[0].since == t0

    # Cycle 3: info recovers (still stopped/auto) → duration stays continuous.
    t2 = t1 + timedelta(minutes=1)
    detect_app_issues([_app("foo", "stopped", boot="auto")], history=history, now=t2)
    assert history["foo"].since == t0


def test_info_blip_on_never_failed_app_does_not_flag() -> None:
    """A stopped app first seen with unknown boot (info blip, never failed before)
    is left unflagged — no false alarm on an app we can't yet classify."""
    history: _History = {}
    issues = detect_app_issues(
        [_app("bar", "stopped", boot="")], history=history, now=NOW
    )
    assert issues == []
    assert history["bar"].since is None


# --- failure detection -------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "boot", "startup", "expected_kind", "detail_substr"),
    [
        ("error", "", "", IssueKind.APP_FAILED, "crashed"),
        ("stopped", "auto", "", IssueKind.APP_FAILED, "boot"),
        ("stopped", "manual", "", None, None),
        # A ``startup: once`` app (e.g. HAOS Configurator) runs and exits by
        # design; being stopped is normal even with boot=auto.
        ("stopped", "auto", "once", None, None),
        # ...but a one-shot that ERRORED (not a clean exit) is still a failure.
        ("error", "", "once", IssueKind.APP_FAILED, "crashed"),
        ("started", "", "", None, None),
    ],
)
def test_single_app_failure_detection(
    state: str,
    boot: str,
    startup: str,
    expected_kind: IssueKind | None,
    detail_substr: str | None,
) -> None:
    issues = detect_app_issues(
        [_app("z", state, boot=boot, startup=startup)], history={}, now=NOW
    )
    if expected_kind is None:
        assert issues == []
        return
    assert len(issues) == 1
    assert issues[0].kind is expected_kind
    assert issues[0].source == "z"
    assert detail_substr is not None and detail_substr in issues[0].detail
    # First sight in a failed state stamps the streak start (drives "for").
    assert issues[0].since == NOW


def test_failed_since_holds_then_resets_on_recovery() -> None:
    history: _History = {}
    detect_app_issues([_app("z", "error")], history=history, now=NOW)
    assert history["z"].since == NOW
    # Still failed a minute later → the streak start is unchanged.
    later = NOW + timedelta(minutes=1)
    issues = detect_app_issues([_app("z", "error")], history=history, now=later)
    assert issues[0].since == NOW
    # Recovers → since cleared, so a later failure dates from the new streak.
    detect_app_issues([_app("z", "started")], history=history, now=later)
    assert history["z"].since is None


def test_only_the_failed_app_is_flagged() -> None:
    issues = detect_app_issues(
        [
            _app("ok", "started"),
            _app("bad", "error"),
            _app("man", "stopped", boot="manual"),
        ],
        history={},
        now=NOW,
    )
    assert [i.source for i in issues] == ["bad"]


# --- instability -------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "threshold", "window", "expected_kind", "expected_flaps"),
    [
        # 3 in-window drops → unstable, superseding a plain failed.
        (
            ["started", "error", "started", "error", "started", "error"],
            3,
            3600,
            IssueKind.APP_UNSTABLE,
            None,
        ),
        # Below the threshold → still just failed.
        (["started", "error", "started", "error"], 3, 3600, IssueKind.APP_FAILED, None),
        # Stuck in error for many cycles is ONE drop, not a growing loop.
        (
            ["started", "error", "error", "error", "error"],
            3,
            3600,
            IssueKind.APP_FAILED,
            1,
        ),
        # started↔stopped (manual toggle / app update) is not a crash loop: no
        # flaps recorded, and a boot-unknown stop isn't a failure.
        (
            ["started", "stopped", "started", "stopped", "started", "stopped"],
            3,
            3600,
            None,
            0,
        ),
        # 3 drops 60s apart but a 90s window keeps only the most recent → failed.
        (
            ["started", "error", "started", "error", "started", "error"],
            3,
            90,
            IssueKind.APP_FAILED,
            None,
        ),
        # Already in error on first observation: a failure, but no transition.
        (["error"], 1, 3600, IssueKind.APP_FAILED, 0),
    ],
)
def test_app_instability_detection(
    states: list[str],
    threshold: int,
    window: int,
    expected_kind: IssueKind | None,
    expected_flaps: int | None,
) -> None:
    issues, history = _feed(states, threshold=threshold, window_seconds=window)
    if expected_kind is None:
        assert issues == []
    else:
        assert issues[0].kind is expected_kind
    if expected_kind is IssueKind.APP_UNSTABLE:
        assert issues[0].since is not None  # dated from the earliest in-window flap
    if expected_flaps is not None:
        assert len(history["z"].flaps) == expected_flaps


# --- exclusion / gc ----------------------------------------------------------


def test_excluded_app_yields_no_issue() -> None:
    history: _History = {}
    issues = detect_app_issues(
        [_app("rclone", "error")],
        history=history,
        now=NOW,
        excluded=frozenset({"rclone"}),
    )
    assert issues == []
    assert "rclone" not in history


def test_absent_app_is_forgotten() -> None:
    history: _History = {}
    detect_app_issues([_app("a", "started")], history=history, now=NOW)
    assert "a" in history
    detect_app_issues([], history=history, now=NOW)
    assert "a" not in history


# --- persistence -------------------------------------------------------------


def test_health_state_serialize_roundtrip() -> None:
    state = {
        "z": AppHealthRecord(
            last_state="error",
            flaps=[NOW, NOW - timedelta(minutes=5)],
            since=NOW - timedelta(hours=1),
        )
    }
    restored = _deserialize_app_health(_serialize_app_health(state))
    assert restored["z"].last_state == "error"
    assert len(restored["z"].flaps) == 2
    assert restored["z"].since == NOW - timedelta(hours=1)


def test_deserialize_tolerates_garbage() -> None:
    assert _deserialize_app_health(None) == {}
    assert _deserialize_app_health("nope") == {}
    assert _deserialize_app_health({"z": "bad"}) == {}
    out = _deserialize_app_health(
        {"z": {"last_state": "error", "flaps": ["not-a-date", NOW.isoformat()]}}
    )
    assert len(out["z"].flaps) == 1  # the unparsable timestamp is dropped


# --- snapshot ----------------------------------------------------------------


async def test_snapshot_empty_without_supervisor(hass: HomeAssistant) -> None:
    """Non-Supervised installs (no hassio component) get an empty snapshot."""
    assert await async_app_snapshot(hass) == []


async def test_snapshot_none_when_list_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Supervisor blip on addons.list() returns None (NOT []), so the caller
    keeps its app-health state instead of wiping it as if there were no apps."""

    class _SupervisorAddons:
        async def list(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("supervisor down")

    class _Client:
        addons = _SupervisorAddons()

    monkeypatch.setattr(mod, "is_hassio", lambda _h: True)
    monkeypatch.setattr(
        "homeassistant.components.hassio.get_supervisor_client", lambda _h: _Client()
    )
    assert await async_app_snapshot(hass) is None


async def test_snapshot_maps_state_and_fetches_boot_for_stopped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _App:
        def __init__(self, slug: str, state: str) -> None:
            self.slug = slug
            self.name = slug
            self.state = state

    class _Info:
        boot = "auto"
        startup = "once"

    class _SupervisorAddons:
        async def list(self):  # type: ignore[no-untyped-def]
            return [
                _App("a", "started"),
                _App("b", "stopped"),
                _App("c", "error"),
            ]

        async def addon_info(self, slug: str) -> _Info:
            return _Info()

    class _Client:
        addons = _SupervisorAddons()

    monkeypatch.setattr(mod, "is_hassio", lambda _h: True)
    monkeypatch.setattr(
        "homeassistant.components.hassio.get_supervisor_client", lambda _h: _Client()
    )

    result = await async_app_snapshot(hass)
    assert result is not None
    snap = {a.slug: a for a in result}
    assert snap["a"].state == "started" and snap["a"].boot == ""
    # boot + startup are fetched only for the stopped app.
    assert snap["b"].state == "stopped" and snap["b"].boot == "auto"
    assert snap["b"].startup == "once"
    assert (
        snap["c"].state == "error" and snap["c"].boot == "" and snap["c"].startup == ""
    )


async def test_snapshot_stopped_app_boot_unknown_when_info_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing addon_info leaves boot unknown, so the stopped app stays
    unflagged that cycle (conservative — no false alarm on manual stops)."""

    class _App:
        slug = "b"
        name = "b"
        state = "stopped"

    class _SupervisorAddons:
        async def list(self):  # type: ignore[no-untyped-def]
            return [_App()]

        async def addon_info(self, slug: str) -> object:
            raise RuntimeError("supervisor busy")

    class _Client:
        addons = _SupervisorAddons()

    monkeypatch.setattr(mod, "is_hassio", lambda _h: True)
    monkeypatch.setattr(
        "homeassistant.components.hassio.get_supervisor_client", lambda _h: _Client()
    )

    snap = await async_app_snapshot(hass)
    assert snap is not None  # list() succeeded; only the per-app info call failed
    assert snap[0].state == "stopped" and snap[0].boot == ""
    # boot="" ≠ "auto", so detection does not flag it.
    assert detect_app_issues(snap, history={}, now=NOW) == []
