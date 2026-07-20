# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Shared vocabulary for the whole integration — the data leaf.

Defines the enums, per-device snapshot, detected issue, downtime record, config
value-object, cycle payload, and formatting/identity helpers. Imports only
``const``, Home Assistant types, and pydantic (the persisted records validate
via it), so any layer can depend on it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, TypedDict

from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.const import ATTR_DEVICE_CLASS
from homeassistant.core import State
from homeassistant.helpers.entity_registry import RegistryEntry
from pydantic import AfterValidator, BaseModel

from .const import (
    CONF_AVAILABILITY_IGNORED_PLATFORMS,
    CONF_EXCLUDED_APPS,
    CONF_EXCLUDED_DEVICE_IDS,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITY_IDS,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_STALENESS_EXCLUDED_DEVICE_IDS,
    CONF_STALENESS_EXCLUDED_INTEGRATIONS,
    DOMAIN,
)


def humanize_duration(seconds: float | None) -> str:
    """Render a duration as a compact, human-friendly string.

    Returns ``"unknown"`` when ``seconds`` is ``None``; otherwise a form such as
    ``"<1m"``, ``"5m"``, ``"2h 5m"`` or ``"1d 3h"``.
    """
    if seconds is None:
        return "unknown"

    total = int(seconds)
    if total < 60:
        return "<1m"

    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    if hours < 24:
        rem_minutes = minutes % 60
        return f"{hours}h {rem_minutes}m"

    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d {rem_hours}h"


class ConnectivityState(StrEnum):
    """Resolved reachability of a device (Layer 2)."""

    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class FaultPhase(StrEnum):
    """The Engine-4 fault state-machine phase (pending→active→holding).

    * ``PENDING`` — not-ok observed, awaiting trigger grace (transient; never
      persisted).
    * ``ACTIVE`` — flagged as a fault.
    * ``HOLDING`` — ok again, awaiting the clear hysteresis before clearing.
    """

    PENDING = "pending"
    ACTIVE = "active"
    HOLDING = "holding"


class IssueKind(StrEnum):
    """The detection engine / category that produced a :class:`VigilIssue`."""

    INTEGRATION_FAILURE = "integration_failure"
    DEVICE_OFFLINE_CONFIRMED = "device_offline_confirmed"
    DEVICE_OFFLINE_NO_SIGNAL = "device_offline_no_signal"
    SILENT_DEVICE = "silent_device"
    # A user-defined watch rule matched an entity whose value is not "ok" (Engine 4).
    DEVICE_FAULT = "device_fault"
    # A Supervisor app is failed/stopped-but-should-run, or restart-looping (Engine 5).
    APP_FAILED = "app_failed"
    APP_UNSTABLE = "app_unstable"


# The two "device offline" kinds, grouped for the devices-offline count and the
# per-integration health rollup.
OFFLINE_KINDS = frozenset(
    {IssueKind.DEVICE_OFFLINE_CONFIRMED, IssueKind.DEVICE_OFFLINE_NO_SIGNAL}
)

# The two app health kinds (Engine 5).
APP_KINDS = frozenset({IssueKind.APP_FAILED, IssueKind.APP_UNSTABLE})


# Config-entry states that are authoritative failure signals (HA has already
# settled these, so Engine 1 needs no grace). Shared by Engine 1 and the
# per-integration health rollup so the two views can't drift apart.
CONFIG_ENTRY_ALERT_STATES: frozenset[ConfigEntryState] = frozenset(
    {
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.MIGRATION_ERROR,
    }
)


def config_entry_is_reportable(entry: ConfigEntry, exclusions: ExclusionConfig) -> bool:
    """Whether Vigil considers a config entry at all.

    Skips Vigil's own entry, disabled entries, ignored-discovery entries, and
    integration-excluded domains.
    """
    if entry.domain == DOMAIN:
        return False
    if entry.disabled_by is not None:
        return False
    if entry.source == SOURCE_IGNORE:
        return False
    if entry.domain in exclusions.integrations:
        return False
    return True


def is_device_excluded(
    device_id: str | None,
    config_entry_domain: str | None,
    exclusions: ExclusionConfig,
) -> bool:
    """Whether a device is excluded by its id or its owning integration."""
    if device_id is not None and device_id in exclusions.device_ids:
        return True
    return (
        config_entry_domain is not None
        and config_entry_domain in exclusions.integrations
    )


def resolved_device_class(
    entry: RegistryEntry, state: State | None = None
) -> str | None:
    """An entity's effective device class in HA-convention precedence: the user
    override (``device_class``) wins, then the integration default
    (``original_device_class``), then the live state attribute."""
    return (
        entry.device_class
        or entry.original_device_class
        or (state.attributes.get(ATTR_DEVICE_CLASS) if state is not None else None)
    )


@dataclass(frozen=True)
class IssueBucket:
    """One issue category: its ``VigilData``/JSON list key and the kinds it holds.

    ``ISSUE_BUCKETS`` is the single source of truth for the detection categories.
    """

    key: str
    kinds: frozenset[IssueKind]


ISSUE_BUCKETS: tuple[IssueBucket, ...] = (
    IssueBucket("integration_failures", frozenset({IssueKind.INTEGRATION_FAILURE})),
    IssueBucket("devices_offline", OFFLINE_KINDS),
    IssueBucket("stale_devices", frozenset({IssueKind.SILENT_DEVICE})),
    IssueBucket("device_faults", frozenset({IssueKind.DEVICE_FAULT})),
    IssueBucket("app_issues", APP_KINDS),
)


def split_issue_buckets(issues: list[VigilIssue]) -> dict[str, list[VigilIssue]]:
    """Partition ``issues`` into the canonical buckets keyed by ``bucket.key``.

    Every bucket key is always present (empty list if none).
    """
    return {
        bucket.key: [i for i in issues if i.kind in bucket.kinds]
        for bucket in ISSUE_BUCKETS
    }


def issue_counts(buckets: Mapping[str, list[VigilIssue]], total: int) -> dict[str, int]:
    """The counts dict: ``total`` plus one entry per bucket key."""
    counts = {"total": total}
    for bucket in ISSUE_BUCKETS:
        counts[bucket.key] = len(buckets[bucket.key])
    return counts


def _as_frozenset(value: Any) -> frozenset[str]:
    """Coerce an options value (a list, or missing) into a frozenset of str."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, Iterable):
        return frozenset(str(item) for item in value)
    return frozenset()


@dataclass(frozen=True)
class ExclusionConfig:
    """User-configured exclusion lists (Layer 4 false-positive suppression).

    A plain value-object so any layer can take one without importing detection.
    """

    domains: frozenset[str]
    entity_ids: frozenset[str]
    device_ids: frozenset[str]
    integrations: frozenset[str]
    # Entity platforms whose entities don't count toward the availability
    # judgement (deployment-specific annotation platforms, user-configured).
    ignored_platforms: frozenset[str] = frozenset()
    # Supervisor app slugs excluded from Engine 5 app health detection.
    apps: frozenset[str] = frozenset()

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> ExclusionConfig:
        """Build an :class:`ExclusionConfig` from a config entry's options."""
        return cls(
            domains=_as_frozenset(options.get(CONF_EXCLUDED_DOMAINS)),
            entity_ids=_as_frozenset(options.get(CONF_EXCLUDED_ENTITY_IDS)),
            device_ids=_as_frozenset(options.get(CONF_EXCLUDED_DEVICE_IDS)),
            integrations=_as_frozenset(options.get(CONF_EXCLUDED_INTEGRATIONS)),
            ignored_platforms=_as_frozenset(
                options.get(CONF_AVAILABILITY_IGNORED_PLATFORMS)
            ),
            apps=_as_frozenset(options.get(CONF_EXCLUDED_APPS)),
        )

    @classmethod
    def staleness_from_options(cls, options: Mapping[str, Any]) -> ExclusionConfig:
        """Build the staleness-scoped exclusions (device ids + integrations only)."""
        return cls(
            domains=frozenset(),
            entity_ids=frozenset(),
            device_ids=_as_frozenset(options.get(CONF_STALENESS_EXCLUDED_DEVICE_IDS)),
            integrations=_as_frozenset(
                options.get(CONF_STALENESS_EXCLUDED_INTEGRATIONS)
            ),
        )


def ensure_aware(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC. Vigil always writes tz-aware timestamps, so
    this only fires on an externally-written or legacy naive persisted row — and
    it keeps that row from crashing later tz-aware arithmetic (``aware - naive``
    raises ``TypeError``, which would fail a whole detection cycle)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# A pydantic datetime field that is coerced to tz-aware UTC on validation, so a
# persisted naive row is defended at the deserialize boundary.
AwareUTC = Annotated[datetime, AfterValidator(ensure_aware)]


class DowntimeRecord(BaseModel):
    """Cross-cycle per-device offline tracking (Engine 2).

    A pydantic model: ``model_validate`` parses/validates a persisted row (see
    :func:`deserialize_downtime`) and ``model_dump(mode="json")`` serializes it.
    """

    since: AwareUTC
    # ``since`` is only a lower bound (recorder had no good state in the lookback
    # window, so it was floored to the window edge). The UI prefixes "≥".
    is_lower_bound: bool = False
    # The recorder has already been consulted for this device, so later cycles
    # don't re-query it (but a recovered-then-failed device gets a fresh record).
    recorder_resolved: bool = False


@dataclass(frozen=True)
class AppInfo:
    """Supervisor app snapshot fed to Engine 5."""

    slug: str
    name: str
    # started / startup / stopped / error / unknown
    state: str
    # auto / manual, or "" when unknown (only fetched for stopped apps).
    boot: str = ""
    # Supervisor startup type (initialize / system / services / application /
    # once), or "" when unknown. ``once`` is a one-shot app that runs and exits
    # by design, so being stopped is normal — not a failure.
    startup: str = ""


@dataclass
class AppHealthRecord:
    """Cross-cycle app health tracking (Engine 5): the last observed state, the
    timestamps of recent healthy→unhealthy transitions (restart flapping), and
    when the current failed streak began (for the reported duration).

    Kept a dataclass (not pydantic): :func:`_deserialize_app_health` deliberately
    salvages a partially-corrupt row (drop an unparsable flap, null a bad
    ``since``) rather than rejecting it whole — a tested behavior pydantic's
    row-level validation would only re-implement at greater length.
    """

    last_state: str
    flaps: list[datetime] = field(default_factory=list)
    # When the app entered its current failed state; None while not failed.
    since: datetime | None = None


@dataclass
class DeviceTuple:
    """Per-device snapshot fed to the detection engines (brief Layer 2 output)."""

    device_id: str
    device_name: str
    config_entry_id: str | None
    config_entry_domain: str | None
    config_entry_title: str | None
    config_entry_state: ConfigEntryState | None
    connectivity_state: ConnectivityState
    connectivity_source: str
    entity_states: list[State]
    all_unavailable: bool
    any_unavailable: bool
    is_battery: bool
    # Connectivity/status meta-signal entity ids (a connectivity binary_sensor, a
    # zwave node_status). Excluded from availability and staleness.
    signal_entity_ids: set[str] = field(default_factory=set)
    # The entity ids that drive the availability judgement — the device's real
    # data telemetry, with signal/annotation/no-state entities removed. The
    # recorder downtime seed queries exactly these, so an annotation entity that
    # stays available can't reset the reconstructed outage clock.
    data_entity_ids: set[str] = field(default_factory=set)
    # When the device's data entities became unavailable (the most recent such
    # transition), derived from entity last_changed. Lets Engine 2 report a device
    # that was already offline before Vigil started. None when not currently
    # (data-)unavailable.
    offline_since: datetime | None = None

    @property
    def integration_label(self) -> str:
        """Human label for the owning integration, falling back to domain."""
        return self.config_entry_title or self.config_entry_domain or "unknown"


class VigilIssueDict(TypedDict):
    """The JSON/wire shape of a VigilIssue, as served at /api/vigil/state.

    Source of truth for the generated frontend VigilIssue type; kept in lockstep
    with VigilIssue.as_dict (mypy enforces it).
    """

    kind: str
    name: str
    integration: str
    detail: str
    since: str | None
    duration_seconds: float | None
    since_is_lower_bound: bool
    source: str
    device_id: str | None
    entity_id: str | None
    config_entry_id: str | None
    domain: str | None


@dataclass
class VigilIssue:
    """A single detected problem, ready for notification / sensors / panel."""

    kind: IssueKind
    name: str
    integration: str
    detail: str
    since: datetime | None = None
    source: str = ""
    device_id: str | None = None
    # The specific entity a DEVICE_FAULT was raised for (watch rules emit one issue
    # per faulted entity), used as the acknowledge identity; unset for device-level
    # kinds.
    entity_id: str | None = None
    config_entry_id: str | None = None
    # Config-entry domain (e.g. "mqtt"). ``integration`` is a human label and may
    # be a title; ``domain`` is the stable key exclusions match against.
    domain: str | None = None
    # True when ``since`` is only a lower bound (the true downtime is older than
    # the recorder lookback window). The UI prefixes the duration with "≥".
    since_is_lower_bound: bool = False

    def duration_seconds(self, now: datetime) -> float | None:
        if self.since is None:
            return None
        return max(0.0, (now - self.since).total_seconds())

    def as_dict(self, now: datetime) -> VigilIssueDict:
        """JSON-serializable form for sensor attributes and the panel feed."""
        return VigilIssueDict(
            kind=str(self.kind),
            name=self.name,
            integration=self.integration,
            detail=self.detail,
            since=self.since.isoformat() if self.since else None,
            duration_seconds=self.duration_seconds(now),
            since_is_lower_bound=self.since_is_lower_bound,
            source=self.source,
            device_id=self.device_id,
            entity_id=self.entity_id,
            config_entry_id=self.config_entry_id,
            domain=self.domain,
        )


def issue_key(issue: VigilIssue) -> str:
    """A stable identity for an issue across detection cycles, for acknowledge.

    Keyed on the target (device or config entry), not the kind, so a device that
    flaps between offline kinds stays "the same issue". A ``DEVICE_FAULT`` is the
    exception: raised per faulted entity, so it's keyed on the entity.
    """
    if issue.kind is IssueKind.DEVICE_FAULT:
        return f"fault:{issue.entity_id or issue.device_id or issue.name}"
    if issue.kind in APP_KINDS:
        # App issues carry the slug in ``source`` as their stable identity.
        return f"app:{issue.source or issue.name}"
    return issue.device_id or issue.config_entry_id or issue.name


class IntegrationHealthRow(TypedDict):
    """One row of the per-integration health table shown in the panel."""

    domain: str
    title: str
    state: str
    healthy: bool
    device_count: int
    offline_count: int
    stale_count: int
    fault_count: int
    failed: bool


class VigilData(TypedDict):
    """Coordinator payload — the result of one detection cycle."""

    issues: list[VigilIssue]
    integration_failures: list[VigilIssue]
    devices_offline: list[VigilIssue]
    stale_devices: list[VigilIssue]
    device_faults: list[VigilIssue]
    app_issues: list[VigilIssue]
    counts: dict[str, int]
    integration_health: list[IntegrationHealthRow]
    last_run: datetime
    healthy: bool
    startup_grace_active: bool


class VigilStateDict(TypedDict):
    """The JSON/wire shape of VigilData, as served at /api/vigil/state.

    Root type the frontend generator introspects: the same buckets as VigilData
    but with issues as VigilIssueDict and last_run as an ISO-8601 string.
    """

    issues: list[VigilIssueDict]
    integration_failures: list[VigilIssueDict]
    devices_offline: list[VigilIssueDict]
    stale_devices: list[VigilIssueDict]
    device_faults: list[VigilIssueDict]
    app_issues: list[VigilIssueDict]
    counts: dict[str, int]
    integration_health: list[IntegrationHealthRow]
    last_run: str
    healthy: bool
    startup_grace_active: bool


def build_vigil_data(
    *,
    issues: list[VigilIssue],
    integration_health: list[IntegrationHealthRow],
    last_run: datetime,
    startup_grace_active: bool,
) -> VigilData:
    """Assemble a :class:`VigilData` payload from a cycle's issues.

    The single construction site: partitions ``issues`` into the canonical
    buckets, derives the counts, and computes ``healthy`` (never claimed during
    startup grace).
    """
    buckets = split_issue_buckets(issues)
    return VigilData(
        issues=issues,
        integration_failures=buckets["integration_failures"],
        devices_offline=buckets["devices_offline"],
        stale_devices=buckets["stale_devices"],
        device_faults=buckets["device_faults"],
        app_issues=buckets["app_issues"],
        counts=issue_counts(buckets, len(issues)),
        integration_health=integration_health,
        last_run=last_run,
        healthy=(not issues) and not startup_grace_active,
        startup_grace_active=startup_grace_active,
    )


def empty_vigil_data(now: datetime) -> VigilData:
    """A no-issues :class:`VigilData` in the honest "starting" shape, for surfaces
    asked to render before the first detection cycle has produced one."""
    return build_vigil_data(
        issues=[], integration_health=[], last_run=now, startup_grace_active=True
    )
