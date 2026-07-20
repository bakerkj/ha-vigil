# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from homeassistant.components import persistent_notification
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from ..const import NOTIFICATION_ID, NOTIFICATION_TITLE
from ..models import IssueKind, VigilData, VigilIssue, humanize_duration, issue_key
from .acknowledgement import AckRepo

__all__ = ["Notifier", "render_message"]


def _duration_for(issue: VigilIssue, now: datetime) -> str:
    """Humanized duration since ``issue.since``, prefixed ``≥`` for a lower bound."""
    duration = humanize_duration(issue.duration_seconds(now))
    if issue.since_is_lower_bound and issue.since is not None:
        return f"≥ {duration}"
    return duration


# Markdown characters that enable injection (links, code spans, inline HTML) from
# an untrusted name. Cosmetic emphasis chars (* _ #) are left alone so names like
# "zwave_js" render normally.
_MD_SPECIALS = "`[]()<>"


def _md(text: str) -> str:
    """Backslash-escape injection-capable markdown specials in untrusted text."""
    out = text
    for ch in _MD_SPECIALS:
        out = out.replace(ch, "\\" + ch)
    return out


# Cap per section so a mass outage can't produce a multi-hundred-line
# notification. The header keeps the FULL count; the card feed carries the detail.
_MAX_PER_SECTION = 20


def _section(
    header: str, issues: list[VigilIssue], fmt: Callable[[VigilIssue], str]
) -> list[str]:
    """Header (with full count), up to ``_MAX_PER_SECTION`` items, and an "…and N
    more" pointer when truncated."""
    lines = ["", header]
    lines.extend(fmt(issue) for issue in issues[:_MAX_PER_SECTION])
    hidden = len(issues) - _MAX_PER_SECTION
    if hidden > 0:
        lines.append(f"• …and {hidden} more — see the Vigil card")
    return lines


def render_message(data: VigilData, now: datetime) -> str:
    """Build the markdown notification body (brief Layer 5 format)."""
    total = data["counts"]["total"]
    lines: list[str] = [f"⚠️ Vigil — {total} issue(s) detected"]

    def _fail_fmt(issue: VigilIssue) -> str:
        # Config entries expose no failure timestamp, so omit the duration.
        if issue.since is not None:
            return (
                f"• {_md(issue.name)} — {issue.source} "
                f"since {_duration_for(issue, now)}"
            )
        return f"• {_md(issue.name)} — {issue.source}"

    # The offline bucket splits into two rendered subsections by kind.
    offline = data["devices_offline"]
    confirmed = [i for i in offline if i.kind == IssueKind.DEVICE_OFFLINE_CONFIRMED]
    no_signal = [i for i in offline if i.kind == IssueKind.DEVICE_OFFLINE_NO_SIGNAL]

    # (issues, header, formatter) in display order; each renders only if non-empty.
    # All bucket keys are always present, so index directly.
    sections: list[tuple[list[VigilIssue], str, Callable[[VigilIssue], str]]] = [
        (
            data["integration_failures"],
            f"INTEGRATION FAILURES ({len(data['integration_failures'])})",
            _fail_fmt,
        ),
        (
            confirmed,
            f"DEVICES OFFLINE — network confirmed ({len(confirmed)})",
            lambda issue: (
                f"• {_md(issue.name)} — {issue.source} DOWN "
                f"{_duration_for(issue, now)} ({_md(issue.integration)})"
            ),
        ),
        (
            no_signal,
            f"DEVICES OFFLINE — no network signal ({len(no_signal)})",
            lambda issue: (
                f"• {_md(issue.name)} — all entities unavailable "
                f"{_duration_for(issue, now)} ({_md(issue.integration)})"
            ),
        ),
        (
            data["stale_devices"],
            f"SILENT DEVICES — network UP, data stale ({len(data['stale_devices'])})",
            lambda issue: (
                f"• {_md(issue.name)} — {issue.detail} ({_md(issue.integration)})"
            ),
        ),
        (
            data["device_faults"],
            f"DEVICE FAULTS — watch rule triggered ({len(data['device_faults'])})",
            lambda issue: (
                f"• {_md(issue.name)} — {_md(issue.detail)} ({_md(issue.integration)})"
            ),
        ),
        (
            data["app_issues"],
            f"APPS ({len(data['app_issues'])})",
            lambda issue: f"• {_md(issue.name)} — {_md(issue.detail)}",
        ),
    ]
    for issues, header, fmt in sections:
        if issues:
            lines += _section(header, issues, fmt)

    return "\n".join(lines)


class Notifier:
    """Owns the single in-place persistent notification and its acknowledge cycle.

    The persisted acknowledged keys live in the injected :class:`AckRepo`; this
    class owns the transient state: ``_notif_keys`` (keys the shown notification
    covers) and ``_self_dismissing`` (marks our own dismissals so the REMOVED
    event isn't mistaken for a user acknowledgement).
    """

    def __init__(self, hass: HomeAssistant, acks: AckRepo) -> None:
        self._hass = hass
        self._acks = acks
        self._notif_keys: frozenset[str] = frozenset()
        self._self_dismissing = False

    def subscribe(self) -> CALLBACK_TYPE:
        """Register for notification add/remove events; return the unsubscribe."""
        return persistent_notification.async_register_callback(
            self._hass, self._on_notifications_updated
        )

    @callback
    def _on_notifications_updated(
        self, update_type: persistent_notification.UpdateType, notifications: object
    ) -> None:
        """Turn a *user* dismissal of our notification into acknowledgements.

        Act only on a REMOVED event for our own id that we did not raise
        (``_self_dismissing``): the shown issues are acknowledged and won't
        re-raise until they clear and return.
        """
        if (
            update_type is persistent_notification.UpdateType.REMOVED
            and isinstance(notifications, dict)
            and NOTIFICATION_ID in notifications
            and not self._self_dismissing
            and self._notif_keys
        ):
            self._acks.set(self._acks.acknowledged | self._notif_keys)
            self._notif_keys = frozenset()
            # Persist now: a user dismissal must survive an unclean shutdown.
            self._hass.async_create_task(
                self._acks.async_persist_now(), "vigil-ack-persist"
            )

    @callback
    def dismiss(self) -> None:
        """Dismiss our notification (idempotent), flagged self-initiated so the
        resulting REMOVED event isn't mistaken for a user acknowledgement."""
        self._self_dismissing = True
        try:
            persistent_notification.async_dismiss(self._hass, NOTIFICATION_ID)
        finally:
            self._self_dismissing = False
        self._notif_keys = frozenset()

    @callback
    def update(self, data: VigilData, now: datetime, *, enabled: bool) -> None:
        """Create/update/dismiss the notification with acknowledge semantics.

        Dismissing acknowledges the shown issues; a new (unacknowledged) issue
        re-raises it. ``enabled`` is the user's notification toggle.
        """
        if data["startup_grace_active"]:
            # Detection paused — show nothing and leave acknowledgements untouched.
            self.dismiss()
            return

        active = frozenset(issue_key(i) for i in data["issues"])

        # An acknowledged issue that has cleared is un-acknowledged so its return
        # alerts again. Do this even when notifications are disabled, so
        # re-enabling isn't silent on an issue that cleared-and-returned.
        pruned = self._acks.acknowledged & active
        if pruned != self._acks.acknowledged:
            self._acks.set(pruned)
            self._acks.persist()

        if not enabled:
            self.dismiss()
            return

        if not active:
            self.dismiss()
            return

        if active - self._acks.acknowledged:
            # At least one new/returned issue → (re)raise showing all current ones.
            persistent_notification.async_create(
                self._hass,
                render_message(data, now),
                title=NOTIFICATION_TITLE,
                notification_id=NOTIFICATION_ID,
            )
            self._notif_keys = active
        elif self._notif_keys:
            # Every remaining active issue is acknowledged → take the still-shown
            # notification down rather than leave stale content displayed.
            self.dismiss()
