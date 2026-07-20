# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.helpers.device_registry import DeviceInfo

DOMAIN = "vigil"
NAME = "Vigil"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

# The two states that mean "no usable value" — a device is reachable but this
# entity is carrying nothing. Shared by inputs + engines 3/4 so the pair isn't
# re-inlined per file.
NO_VALUE_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})


def vigil_device_info(entry_id: str) -> DeviceInfo:
    """Shared DeviceInfo grouping every Vigil entity under one device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=NAME,
        manufacturer="Vigil",
        model="Health monitor",
    )


# Fixed unique id keeps the config flow to one entry per HA install.
UNIQUE_ID = "vigil_singleton"

# Stable id makes every cycle update the same notification in place.
NOTIFICATION_ID = "vigil_issues"
NOTIFICATION_TITLE = NAME

# Row-level SQLite under <config>/.storage/: writes proportional to what changed.
STORAGE_SQLITE_FILE = "vigil_intervals.db"

# Keys in the interval store's shared key-value ``state`` table, so all Vigil
# state lands in the one configured store rather than separate HA .storage files.
STATE_ACK_KEY = "acknowledged"
STATE_FAULT_KEY = "faults"
STATE_DOWNTIME_KEY = "downtime"
STATE_APP_KEY = "app_health"

# Recorder-derived true downtime: look back this many days for a device's last
# good reading (the real outage start, surviving restarts). Each device is
# queried at most once, so the offline cohort resolves in one batched query.
RECORDER_LOOKBACK_DAYS = 7
# A value RESTORED at restart lands just after a recorder run start; treat a
# "good" state within this window of any run start as a restart artifact, so a
# long-dead device floors to ">= window" instead of reporting the restart.
RESTART_ARTIFACT_WINDOW_SECONDS = 300
# Cap entity_ids per history query so the first post-reboot cycle (whole cohort
# briefly offline) doesn't issue one enormous get_significant_states call.
RECORDER_ENTITY_CHUNK = 250
# Declarative watch rules (Engine 4): a deployment-specific file in the HA config
# dir, never shipped with the integration. Absent = feature off.
# The unified config file (watch + ignore rules), in the HA config dir. The
# legacy watch-only file is still read when this one is absent (back-compat).
VIGIL_CONFIG_FILE = "vigil.yaml"
WATCH_RULES_FILE = "vigil_watch.yaml"

# HA boot time; recorder downtime reconstruction ignores states at/after this
# instant as restart artifacts (last_changed resets on boot). Survives reloads;
# unset if Vigil was installed post-boot.
DATA_BOOT_TIME = "vigil_boot_time"
# When HA finished starting. Upper edge of the restart-artifact band: staggered
# boot setup means a device already down can report "off" minutes after the boot
# anchor, so anything up to here is a boot artifact and a later drop is real.
DATA_HA_STARTED_AT = "vigil_ha_started_at"
# States that are not a real reading for downtime purposes; some integrations
# surface a literal "None"/"" placeholder.
RECORDER_NON_VALUE_STATES = frozenset({"unavailable", "unknown", "None", "none", ""})
# Lovelace card, registered as a global frontend resource; added via
# ``type: custom:vigil-card``.
CARD_FILENAME = "vigil-card.js"
STATIC_PATH = "/vigil_static"
API_STATE_PATH = "/api/vigil/state"

# --- Config / options keys ---------------------------------------------------

CONF_SCAN_INTERVAL = "scan_interval"
CONF_GRACE_PERIOD_MINUTES = "grace_period_minutes"
CONF_STALENESS_MULTIPLIER = "staleness_multiplier"
CONF_STARTUP_IGNORE_SECONDS = "startup_ignore_seconds"
CONF_EXCLUDED_DOMAINS = "excluded_domains"
CONF_EXCLUDED_ENTITY_IDS = "excluded_entity_ids"
CONF_EXCLUDED_DEVICE_IDS = "excluded_device_ids"
CONF_EXCLUDED_INTEGRATIONS = "excluded_integrations"
CONF_BATTERY_GRACE_MULTIPLIER = "battery_device_grace_multiplier"
CONF_ENABLE_NOTIFICATION = "enable_notification"
# Whether Engine 5 polls the Supervisor for app health each cycle.
CONF_ENABLE_APP_MONITORING = "enable_app_monitoring"
# Staleness-scoped exclusions: drop a device/integration from Engine 3 detection
# only, keeping it in offline (Engine 2) detection — for sources that legitimately
# go quiet when idle but whose total disappearance should still flag.
CONF_STALENESS_EXCLUDED_INTEGRATIONS = "staleness_excluded_integrations"
CONF_STALENESS_EXCLUDED_DEVICE_IDS = "staleness_excluded_device_ids"
# Deployment-specific platforms whose entities annotate another integration's
# devices with metadata that stays available when the device is offline (and so
# would mask an outage). Matched on RegistryEntry.platform.
CONF_AVAILABILITY_IGNORED_PLATFORMS = "availability_ignored_platforms"
CONF_RECORDER_LOOKBACK_DAYS = "recorder_lookback_days"
# Supervisor app slugs to exclude from Engine 5 app health detection.
CONF_EXCLUDED_APPS = "excluded_apps"
# Optional SQLAlchemy URL persisting learned intervals in an external DB over a
# Vigil-owned connection, separate from the recorder. Blank = local SQLite.
CONF_INTERVAL_STORE_URL = "interval_store_url"

# --- Defaults ----------------------------------------------------------------

DEFAULT_SCAN_INTERVAL = 60
DEFAULT_GRACE_PERIOD_MINUTES = 15
DEFAULT_STALENESS_MULTIPLIER = 3.0
DEFAULT_STARTUP_IGNORE_SECONDS = 300
DEFAULT_BATTERY_GRACE_MULTIPLIER = 2.0
# 0 = AUTO: match the recorder's own ``purge_keep_days`` (read at runtime), so
# reconstruction spans exactly the retained history. An explicit 1..MAX overrides;
# RECORDER_LOOKBACK_DAYS (7) is the fallback if retention can't be read.
DEFAULT_RECORDER_LOOKBACK_DAYS = 0
# Off = no notification created/updated (existing one dismissed); panel and
# sensors still work.
DEFAULT_ENABLE_NOTIFICATION = False
# On by default; a no-op anyway on non-Supervised installs (empty snapshot).
DEFAULT_ENABLE_APP_MONITORING = True
# Blank = local SQLite; a SQLAlchemy URL selects the external-DB backend.
DEFAULT_INTERVAL_STORE_URL = ""

# --- Bounds (config flow validation) -----------------------------------------

MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 3600
MIN_GRACE_PERIOD_MINUTES = 1
MAX_GRACE_PERIOD_MINUTES = 1440
MIN_STALENESS_MULTIPLIER = 1.5
MAX_STALENESS_MULTIPLIER = 50.0
MIN_STARTUP_IGNORE_SECONDS = 0
MAX_STARTUP_IGNORE_SECONDS = 3600
MIN_BATTERY_GRACE_MULTIPLIER = 1.0
MAX_BATTERY_GRACE_MULTIPLIER = 10.0
MIN_RECORDER_LOOKBACK_DAYS = 0  # 0 = auto (match recorder purge_keep_days)
MAX_RECORDER_LOOKBACK_DAYS = 90

# --- Interval learning (Engine 3) --------------------------------------------

# Engine 3 learns cadence over a multi-day horizon, keeping each day's LONGEST
# inter-report gap; the expected interval is a high percentile of those daily
# maxes. This captures a bursty sensor's real quiet capacity (e.g. a motion
# sensor's overnight gap) that a count window never retains, and the high
# percentile drops a single anomalous day (e.g. an HA restart).
LEARN_HORIZON_DAYS = 90
# Don't trust an entity's cadence until observed at least this long — enough to
# have seen a full daily activity cycle.
LEARN_WARMUP_DAYS = 2
# Nearest-rank percentile of the per-day max gaps. learned_interval also clamps
# the index to n-2 so the largest day is always dropped once >= 2 days exist.
LEARN_INTERVAL_PERCENTILE = 0.90

# Fallback expected update interval (seconds) when nothing is learned yet, keyed
# by entity domain then device_class.
HEURISTIC_DEFAULT_INTERVAL = 900.0  # 15 min

_HEURISTIC_BY_DOMAIN_CLASS: dict[str, dict[str, float]] = {
    "binary_sensor": {
        "motion": 300.0,
        "occupancy": 300.0,
        "door": 300.0,
        "window": 300.0,
        "opening": 300.0,
        "garage_door": 300.0,
    },
    "sensor": {
        "temperature": 600.0,
        "humidity": 600.0,
        "power": 120.0,
        "energy": 120.0,
        "current": 120.0,
        "voltage": 120.0,
    },
}

_HEURISTIC_BY_DOMAIN: dict[str, float] = {
    "weather": 1800.0,
}


def heuristic_interval(domain: str, device_class: str | None) -> float:
    """Best-guess expected update interval (seconds), used only when the learner
    has no learned interval yet."""
    by_class = _HEURISTIC_BY_DOMAIN_CLASS.get(domain)
    if by_class is not None and device_class is not None:
        learned = by_class.get(device_class)
        if learned is not None:
            return learned
    return _HEURISTIC_BY_DOMAIN.get(domain, HEURISTIC_DEFAULT_INTERVAL)


# --- App health (Engine 5) ------------------------------------------------

# An app is "restart-looping" if it crashes (running → error) this many times
# within the trailing window. Clean stops (manual toggles, app updates) don't
# count. Detection is poll-cadence bound: a crash that recovers within one scan
# interval isn't seen, so this catches apps that stay in error for at least a
# cycle, repeatedly.
APP_UNSTABLE_WINDOW_SECONDS = 1800
APP_UNSTABLE_THRESHOLD = 3


# Domains that hold no live device state, so they must not count toward the
# all-entities-unavailable judgement: ``update`` reports "off" while a node is
# dead, ``button`` carries no telemetry, and ``device_tracker`` reports presence
# ("where"), never liveness. Genuine diagnostic sensors (RSSI, battery) are NOT
# excluded — they go unavailable with the device and are good signals.
AVAILABILITY_IGNORED_DOMAINS = frozenset({"update", "button", "device_tracker"})

# Annotation platforms are deployment-specific, supplied via the user option
# CONF_AVAILABILITY_IGNORED_PLATFORMS rather than hardcoded here.


# --- Connectivity heuristics (Layer 2) ---------------------------------------

# zwave_js exposes a per-node ``sensor`` whose unique_id ends with this suffix
# and whose state is one of the node-status names below.
ZWAVE_NODE_STATUS_SUFFIX = ".node_status"
ZWAVE_STATUS_DOWN = frozenset({"dead"})
# ``asleep`` is normal for battery nodes — treat as UNKNOWN (extended grace).
ZWAVE_STATUS_UNKNOWN = frozenset({"asleep"})
ZWAVE_STATUS_UP = frozenset({"alive", "awake"})

# device_tracker entities from these integrations are router/AP/switch presence
# sources matched to a device by MAC.
MAC_ROUTER_PLATFORMS = frozenset(
    {
        "unifi",
        "aruba",
        "aruba_instant_ap",
        "omada",
        "tplink",
        "tplink_omada",
        "mikrotik",
        "netgear",
        "asuswrt",
        "fritz",
        "keenetic_ndms2",
        "huawei_lte",
        "ubus",
        "eero",
        "fing",
        "switch_port_pro",
    }
)


def merged_options(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge a config entry's ``data`` and ``options`` (options win)."""
    return {**data, **options}
