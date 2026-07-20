# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The Vigil detection coordinator.

Owns the timing and IO of a cycle: builds the per-device tuples, seeds downtime
from the recorder, hands a :class:`.context.CycleContext` to
:func:`.pipeline.run_detection`, then does learner bookkeeping, persists the
Engine-4 fault state, and drives the persistent notification. Cross-cycle
in-memory state lives here; persisted state lives in the AckRepo / RuleFaultRepo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

import voluptuous as vol
from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    Event,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .detection.inputs import build_device_tuples
from .const import (
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_ENABLE_APP_MONITORING,
    CONF_ENABLE_NOTIFICATION,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_RECORDER_LOOKBACK_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_STALENESS_MULTIPLIER,
    CONF_STARTUP_IGNORE_SECONDS,
    DATA_BOOT_TIME,
    DATA_HA_STARTED_AT,
    DEFAULT_BATTERY_GRACE_MULTIPLIER,
    DEFAULT_ENABLE_APP_MONITORING,
    DEFAULT_ENABLE_NOTIFICATION,
    DEFAULT_GRACE_PERIOD_MINUTES,
    DEFAULT_RECORDER_LOOKBACK_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALENESS_MULTIPLIER,
    DEFAULT_STARTUP_IGNORE_SECONDS,
    DOMAIN,
    LEARN_HORIZON_DAYS,
    MAX_RECORDER_LOOKBACK_DAYS,
    RECORDER_LOOKBACK_DAYS,
    merged_options,
)
from .detection.engines.engine2_unavailability import (
    DowntimeRepo,
    async_seed_downtime,
    is_offline,
)
from .detection.engines.engine5_apps import AppHealthRepo, async_app_snapshot
from .detection.engines.watch_config import RuleFaultRepo, VigilConfigStore
from .context import CycleContext
from .history.recorder import async_recorder_interval_aggregate
from .pipeline import run_detection
from .models import (
    AppInfo,
    DowntimeRecord,
    ExclusionConfig,
    VigilData,
)
from .reporting.acknowledgement import AckRepo
from .reporting.notification import Notifier
from .storage import StoreRepo

if TYPE_CHECKING:
    from .learning.interval_learner import IntervalLearner

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


class VigilCoordinator(DataUpdateCoordinator[VigilData]):
    """Drives the periodic detection cycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        learner: IntervalLearner,
    ) -> None:
        self.entry = entry
        self.learner = learner
        # Snapshot is safe: the options-update listener forces a full reload.
        self._options = merged_options(entry.data, entry.options)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=self._opt_int(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        # Per-device offline tracking across cycles. Persisted as the
        # recorder-blind fallback; the recorder seed still wins where it can.
        self._downtime_repo = DowntimeRepo(hass, learner.store)
        # Devices seen UP at least once this session. A device that later drops
        # was observed live, so the recorder seed must not reconstruct it — only
        # devices offline across the restart need reconstruction.
        self._observed_up: set[str] = set()
        # One-shot guard: the learner is seeded from the recorder once per
        # session, in the background, after the first cycle.
        self._learner_seeded = False
        # In-flight seed task so a reload can cancel it before a stale write with
        # the discarded learner regresses the on-disk watermark.
        self._seed_task: asyncio.Task[None] | None = None
        # Notification acknowledgement (Layer 5). Tie the subscription to the
        # config entry so HA releases it even if setup later fails (first_refresh
        # raising ConfigEntryNotReady never calls async_teardown).
        self._acks = AckRepo(hass, learner.store)
        self._notifier = Notifier(hass, self._acks)
        entry.async_on_unload(self._notifier.subscribe())
        # Startup-grace anchor: suppress noise while HA is still loading. Use
        # CoreState.running, not hass.is_running (True during starting too, which
        # would defeat the grace exactly during startup).
        self._setup_time = dt_util.utcnow()
        self._ha_started = hass.state is CoreState.running
        self._unsub_started: CALLBACK_TYPE | None = None
        if not self._ha_started:
            self._unsub_started = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
            )
        # Boot-time anchor for downtime reconstruction: if set up while HA is
        # still starting, this setup instant is ~boot. Stashed so it survives
        # reloads; unset if Vigil was installed after boot.
        if not self._ha_started and DATA_BOOT_TIME not in hass.data:
            hass.data[DATA_BOOT_TIME] = self._setup_time
        self._boot_time: datetime | None = hass.data.get(DATA_BOOT_TIME)
        # When HA finished starting — the upper edge of the signal-only
        # restart-artifact band (see async_seed_downtime). None until then.
        self._ha_started_at: datetime | None = hass.data.get(DATA_HA_STARTED_AT)
        # Engine 4 — declarative watch rules from ``vigil.yaml``. The
        # RuleFaultRepo owns the cross-cycle per-rule trigger/clear debounce map.
        self._config_store = VigilConfigStore(hass)
        self._faults = RuleFaultRepo(hass, learner.store)
        # Engine 5 — Supervisor app health. The repo owns the cross-cycle
        # per-app flap state; the snapshot fetch no-ops on non-Supervised.
        self._app_health = AppHealthRepo(hass, learner.store)

    @callback
    def _on_ha_started(self, _event: Event) -> None:
        self._ha_started = True
        self._unsub_started = None
        self._ha_started_at = self.hass.data.setdefault(
            DATA_HA_STARTED_AT, dt_util.utcnow()
        )

    @callback
    def async_teardown(self) -> None:
        """Release listeners and clear the notification on unload."""
        if self._seed_task is not None:
            self._seed_task.cancel()
            self._seed_task = None
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        # The notification subscription is released via entry.async_on_unload.
        self._notifier.dismiss()

    # --- notification acknowledgement -------------------------------------
    # These delegate to the repos; they stay on the coordinator as its public API.

    @property
    def _acknowledged(self) -> set[str]:
        """The acknowledged issue-key set (delegates to the AckRepo)."""
        return self._acks.acknowledged

    @property
    def _downtime(self) -> dict[str, DowntimeRecord]:
        """The live per-device downtime map (delegates to the DowntimeRepo)."""
        return self._downtime_repo.state

    @property
    def state_repos(self) -> tuple[tuple[str, StoreRepo[Any]], ...]:
        """The persisted repos with their unload labels — loaded at setup and
        flushed on unload (before the shared store is closed)."""
        return (
            ("acknowledged", self._acks),
            ("fault", self._faults),
            ("downtime", self._downtime_repo),
            ("app-health", self._app_health),
        )

    async def async_load_state(self) -> None:
        """Rehydrate every persisted repo from the store (at setup), so a restart
        doesn't re-raise dismissed alerts, reset ongoing faults, or lose an
        outage/flap start."""
        for _label, repo in self.state_repos:
            await repo.async_load()

    async def async_clear_acknowledgements(self) -> None:
        """Forget all acknowledgements so every active issue re-surfaces.

        Backs the "Clear acknowledgements" button. Persists immediately and
        refreshes so the notification is re-raised this cycle.
        """
        if self._acks.acknowledged:
            self._acks.set(set())
            await self._acks.async_persist_now()
        await self.async_refresh()

    def _opt(
        self,
        key: str,
        default: _T,
        convert: Callable[[Any], _T],
        errors: type[Exception] | tuple[type[Exception], ...],
    ) -> _T:
        """Read an option, coerce it via ``convert``, fall back to ``default`` when
        the value is missing or ``convert`` rejects it (raising ``errors``)."""
        try:
            return convert(self._options.get(key, default))
        except errors:
            return default

    def _opt_int(self, key: str, default: int) -> int:
        return self._opt(key, default, lambda v: int(float(v)), (TypeError, ValueError))

    def _opt_float(self, key: str, default: float) -> float:
        return self._opt(key, default, float, (TypeError, ValueError))

    def _opt_bool(self, key: str, default: bool) -> bool:
        return self._opt(key, default, cv.boolean, vol.Invalid)

    def _effective_lookback_days(self) -> int:
        """Days of recorder history the downtime reconstruction looks back.

        An explicit option (1..MAX) wins. The default 0 means AUTO: match the
        recorder's configured retention (``purge_keep_days``) so Vigil
        reconstructs over exactly what's retained. Falls back to
        RECORDER_LOOKBACK_DAYS if the recorder can't be read.
        """
        configured = self._opt_int(
            CONF_RECORDER_LOOKBACK_DAYS, DEFAULT_RECORDER_LOOKBACK_DAYS
        )
        if configured > 0:
            return configured
        try:
            keep_days = int(get_instance(self.hass).keep_days)
        except Exception:  # noqa: BLE001 - recorder may be absent/not ready
            return RECORDER_LOOKBACK_DAYS
        return max(1, min(keep_days, MAX_RECORDER_LOOKBACK_DAYS))

    def _startup_grace_active(self, now: datetime) -> bool:
        ignore_seconds = self._opt_int(
            CONF_STARTUP_IGNORE_SECONDS, DEFAULT_STARTUP_IGNORE_SECONDS
        )
        if ignore_seconds <= 0 or self._ha_started:
            return False
        return (now - self._setup_time).total_seconds() < ignore_seconds

    async def _async_update_data(self) -> VigilData:
        now = dt_util.utcnow()
        exclusions = ExclusionConfig.from_options(self._options)
        # Capture the startup-grace decision up front: HA may finish starting
        # during the recorder await below, but a cycle that began inside the
        # grace window must stay suppressed for its whole duration.
        startup_grace = self._startup_grace_active(now)

        # Layer 2 — per-device tuples, with downtime seeded from the recorder
        # BEFORE Engine 2 reads it so a device already down before an HA restart
        # isn't given a fresh grace. No-op without a recorder.
        # vigil.yaml ignore rules: entities not to treat as connectivity signals.
        ignore_connectivity = await self._config_store.async_get_ignore_connectivity()
        tuples = build_device_tuples(
            self.hass, exclusions, ignore_connectivity=ignore_connectivity
        )
        # Record which devices are UP this cycle before seeding: a device we've
        # ever seen up that later drops is a live-observed outage (skip the
        # recorder), not one straddling the restart.
        self._observed_up.update(t.device_id for t in tuples if not is_offline(t))
        await async_seed_downtime(
            self.hass,
            tuples,
            self._downtime,
            boot_time=self._boot_time,
            ha_started=self._ha_started,
            ha_started_at=self._ha_started_at,
            lookback=timedelta(days=self._effective_lookback_days()),
            now=now,
            observed_up=self._observed_up,
        )

        # Engine 5 input — the Supervisor app snapshot. Empty on non-Supervised
        # or when app monitoring is off; None when the Supervisor read failed
        # (the pipeline then skips Engine 5 and keeps the app-health state).
        apps: list[AppInfo] | None = (
            await async_app_snapshot(self.hass)
            if self._opt_bool(CONF_ENABLE_APP_MONITORING, DEFAULT_ENABLE_APP_MONITORING)
            else []
        )

        # Compose the cycle: hand the pipeline an immutable snapshot plus live
        # handles to the stateful collaborators.
        ctx = CycleContext(
            hass=self.hass,
            now=now,
            exclusions=exclusions,
            staleness_exclusions=ExclusionConfig.staleness_from_options(self._options),
            startup_grace_active=startup_grace,
            tuples=tuples,
            downtime=self._downtime,
            boot_time=self._boot_time,
            learner=self.learner,
            fault_state=self._faults.state,
            config_store=self._config_store,
            grace_period=timedelta(
                minutes=self._opt_float(
                    CONF_GRACE_PERIOD_MINUTES, DEFAULT_GRACE_PERIOD_MINUTES
                )
            ),
            battery_multiplier=self._opt_float(
                CONF_BATTERY_GRACE_MULTIPLIER, DEFAULT_BATTERY_GRACE_MULTIPLIER
            ),
            staleness_multiplier=self._opt_float(
                CONF_STALENESS_MULTIPLIER, DEFAULT_STALENESS_MULTIPLIER
            ),
            apps=apps,
            app_health=self._app_health.state,
        )
        data = await run_detection(ctx)

        # Learner bookkeeping, after the pipeline: forget absent entities, persist
        # this cycle's learning, and once per session seed from recorder history
        # in the background so a fresh install skips the multi-day warmup.
        self.learner.purge_absent(
            {s.entity_id for t in tuples for s in t.entity_states}
        )
        await self.learner.async_flush(now)
        if not self._learner_seeded:
            self._learner_seeded = True
            entity_ids = self.learner.known_entities()
            if entity_ids:
                self._seed_task = self.hass.async_create_background_task(
                    self._async_seed_learner(entity_ids, now),
                    name="vigil_seed_learner",
                )

        # Persist the Engine-4 fault-debounce state and the downtime records (each
        # writes only when it actually changed).
        self._faults.persist()
        self._downtime_repo.persist()
        self._app_health.persist()

        self._notifier.update(
            data,
            now,
            enabled=self._opt_bool(
                CONF_ENABLE_NOTIFICATION, DEFAULT_ENABLE_NOTIFICATION
            ),
        )
        return data

    async def _async_seed_learner(self, entity_ids: list[str], now: datetime) -> None:
        """Seed or catch up the interval learner from recorder history.

        A fresh store (no watermark) bootstraps over the full horizon; an existing
        store catches up from its watermark. Best-effort: on failure the learner
        warms up live and the next boot retries.
        """
        watermark = self.learner.watermark()
        if watermark is not None:
            # Overlap the catch-up window back one day so the gap straddling the
            # watermark isn't dropped (the window function gives the first
            # in-window row no predecessor). Idempotent: buckets merge by MAX.
            start = watermark - timedelta(days=1)
            mode = "catch-up"
        else:
            start = now - timedelta(days=LEARN_HORIZON_DAYS)
            mode = "bootstrap"
        try:
            buckets, last_good = await async_recorder_interval_aggregate(
                self.hass, entity_ids, start, now
            )
        except Exception:  # noqa: BLE001 - recorder seed is best-effort
            _LOGGER.warning(
                "Vigil: interval learner %s from recorder failed; warming up live "
                "and retrying next start",
                mode,
                exc_info=True,
            )
            return
        self.learner.ingest(buckets, last_good, now)
        await self.learner.async_flush(now)
        _LOGGER.info(
            "Vigil: interval learner %s complete — %d entities, %d day-buckets",
            mode,
            len(entity_ids),
            len(buckets),
        )
