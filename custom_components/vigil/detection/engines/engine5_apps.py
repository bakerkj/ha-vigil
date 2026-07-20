# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Engine 5 — Supervisor app health.

Flags apps that are failed (``error``, or ``stopped`` while set to start on
boot) or restart-looping (dropping from a running state repeatedly within a
window). A ``startup: once`` app is one-shot — it runs and exits by design, so a
stopped one is not flagged. The snapshot fetch is gated on Supervisor being
present, so the whole engine no-ops on Container/Core installs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio
from homeassistant.util import dt as dt_util

from ...const import (
    APP_UNSTABLE_THRESHOLD,
    APP_UNSTABLE_WINDOW_SECONDS,
    STATE_APP_KEY,
)
from ...models import AppHealthRecord, AppInfo, IssueKind, VigilIssue, ensure_aware
from ...storage import StateStore, StoreRepo

_LOGGER = logging.getLogger(__name__)

# States in which an app is considered up. A drop out of these is a flap.
_HEALTHY_STATES = frozenset({"started", "startup"})
_APP_LABEL = "App"


async def async_app_snapshot(hass: HomeAssistant) -> list[AppInfo] | None:
    """Read the installed apps and their states from the Supervisor.

    Returns an empty list when Supervisor is genuinely app-less or absent
    (Container/Core), but ``None`` when the Supervisor read FAILS (busy /
    mid-update / not-ready-at-boot). The distinction matters: a caller must NOT
    treat a read failure as "no apps", or it would wipe the cross-cycle app-health
    state (flap counts, failure ``since``) on a transient blip. App health is
    best-effort and must never break a detection cycle. The per-app ``info`` call
    (for the boot policy) is made only for *stopped* apps, so a healthy install
    costs one list call per cycle.
    """
    if not is_hassio(hass):
        return []
    from homeassistant.components.hassio import get_supervisor_client

    client = get_supervisor_client(hass)
    try:
        installed = await client.addons.list()
    except Exception:  # noqa: BLE001 - Supervisor blip must not break the cycle
        _LOGGER.warning(
            "Vigil: could not read the app list from Supervisor; keeping last "
            "known app-health state this cycle",
            exc_info=True,
        )
        return None

    # Boot policy + startup type are needed only for stopped apps (auto = should
    # be running; startup=once = one-shot, stopped is normal). Fetch them
    # CONCURRENTLY so several intentionally-stopped apps don't cost N sequential
    # Supervisor round-trips per cycle. If an info call fails, both stay "" and the
    # app is left unflagged this cycle — conservative on purpose: a blip retries
    # next cycle, and never flagging on unknown boot avoids false alarms on
    # intentionally (manually) stopped apps.
    async def _boot_info(slug: str) -> tuple[str, str]:
        try:
            info = await client.addons.addon_info(slug)
            return str(info.boot), str(info.startup)
        except Exception:  # noqa: BLE001 - unknown; skip this app
            _LOGGER.debug("Vigil: app info for %s failed", slug, exc_info=True)
            return "", ""

    stopped = [app for app in installed if str(app.state) == "stopped"]
    info_by_slug = dict(
        zip(
            (app.slug for app in stopped),
            await asyncio.gather(*(_boot_info(app.slug) for app in stopped)),
        )
    )
    return [
        AppInfo(
            slug=app.slug,
            name=app.name,
            state=str(app.state),
            boot=info_by_slug.get(app.slug, ("", ""))[0],
            startup=info_by_slug.get(app.slug, ("", ""))[1],
        )
        for app in installed
    ]


def detect_app_issues(
    snapshot: list[AppInfo],
    *,
    history: dict[str, AppHealthRecord],
    now: datetime,
    excluded: frozenset[str] = frozenset(),
    window_seconds: int = APP_UNSTABLE_WINDOW_SECONDS,
    threshold: int = APP_UNSTABLE_THRESHOLD,
) -> list[VigilIssue]:
    """Engine 5 — flag failed and restart-looping apps.

    Mutates ``history`` in place: records each app's last state and the
    timestamps of recent healthy→unhealthy transitions, pruned to ``window``, and
    forgets apps no longer present. An unstable app supersedes a plain
    failure so each app yields at most one issue.
    """
    issues: list[VigilIssue] = []
    cutoff = now - timedelta(seconds=window_seconds)
    seen: set[str] = set()
    for app in snapshot:
        if app.slug in excluded:
            continue
        seen.add(app.slug)
        # A ``startup: once`` app is one-shot — it runs and exits by design, so a
        # stopped one is NOT a failure. An ``error``-state one-shot still counts
        # (it errored during its run, not a clean exit).
        failed = app.state == "error" or (
            app.state == "stopped" and app.boot == "auto" and app.startup != "once"
        )
        rec = history.get(app.slug)
        # A per-app ``info`` read failure returns boot="" for a stopped app (see
        # async_app_snapshot) — that is UNKNOWN, not recovered. If the app was
        # already failing, keep it failed: otherwise a transient Supervisor blip
        # would clear its ``since`` and drop the issue, and the next good read would
        # re-alert an already-acknowledged failure (a false re-notification).
        info_unknown = app.state == "stopped" and app.boot == "" and app.startup == ""
        if info_unknown and rec is not None and rec.since is not None:
            failed = True
        if rec is None:
            rec = AppHealthRecord(last_state=app.state, since=now if failed else None)
            history[app.slug] = rec
        else:
            # Count only a crash (running → error) as a flap. A clean stop is
            # usually intentional — a manual toggle or an app update, which
            # cycles stopped→started — and must not read as a restart loop.
            if rec.last_state in _HEALTHY_STATES and app.state == "error":
                rec.flaps.append(now)
            # Track when the CURRENT failed streak began, for the "for" duration:
            # stamp on entry, keep while failed, clear on recovery.
            if failed and rec.since is None:
                rec.since = now
            elif not failed:
                rec.since = None
            rec.last_state = app.state
        rec.flaps = [t for t in rec.flaps if t >= cutoff]

        if len(rec.flaps) >= threshold:
            issues.append(
                VigilIssue(
                    kind=IssueKind.APP_UNSTABLE,
                    name=app.name,
                    integration=_APP_LABEL,
                    detail=(
                        f"restarting repeatedly ({len(rec.flaps)}× recently; "
                        f"now {app.state})"
                    ),
                    since=min(rec.flaps),
                    source=app.slug,
                )
            )
        elif failed:
            detail = (
                "crashed (error state)"
                if app.state == "error"
                else "stopped but set to start on boot"
            )
            issues.append(
                VigilIssue(
                    kind=IssueKind.APP_FAILED,
                    name=app.name,
                    integration=_APP_LABEL,
                    detail=detail,
                    since=rec.since,
                    source=app.slug,
                )
            )

    for slug in list(history):
        if slug not in seen:
            del history[slug]
    return issues


def _serialize_app_health(state: dict[str, AppHealthRecord]) -> dict[str, Any]:
    return {
        slug: {
            "last_state": rec.last_state,
            "flaps": [t.isoformat() for t in rec.flaps],
            "since": rec.since.isoformat() if rec.since else None,
        }
        for slug, rec in state.items()
    }


def _deserialize_app_health(data: Any) -> dict[str, AppHealthRecord]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, AppHealthRecord] = {}
    for slug, raw in data.items():
        if not isinstance(raw, dict):
            continue
        flaps: list[datetime] = []
        for item in raw.get("flaps") or []:
            ts = dt_util.parse_datetime(str(item))
            if ts is not None:
                flaps.append(ensure_aware(ts))
        raw_since = raw.get("since")
        parsed_since = dt_util.parse_datetime(str(raw_since)) if raw_since else None
        since = ensure_aware(parsed_since) if parsed_since is not None else None
        out[str(slug)] = AppHealthRecord(
            last_state=str(raw.get("last_state", "unknown")), flaps=flaps, since=since
        )
    return out


class AppHealthRepo(StoreRepo[dict[str, AppHealthRecord]]):
    """Cross-cycle app health state, persisted to the shared ``state`` table."""

    def __init__(self, hass: HomeAssistant, store: StateStore) -> None:
        super().__init__(
            hass,
            store,
            key=STATE_APP_KEY,
            initial={},
            serialize=_serialize_app_health,
            deserialize=_deserialize_app_health,
        )
