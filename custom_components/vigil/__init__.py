# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CARD_FILENAME,
    DOMAIN,
    PLATFORMS,
    STATIC_PATH,
    merged_options,
)

if TYPE_CHECKING:
    from homeassistant.components.lovelace.resources import ResourceStorageCollection

    from .coordinator import VigilCoordinator

_LOGGER = logging.getLogger(__name__)

# Config-entry-only integration — no YAML configuration is accepted. Satisfies
# hassfest's CONFIG_SCHEMA requirement for an integration that defines async_setup.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# hass.data keys.
DATA_STATIC_REGISTERED = f"{DOMAIN}_static_registered"
DATA_VIEW_REGISTERED = f"{DOMAIN}_view_registered"
DATA_CARD_REGISTERED = f"{DOMAIN}_card_registered"


@dataclass
class VigilEntryData:
    """Per-entry runtime objects (stored on ``ConfigEntry.runtime_data``)."""

    coordinator: VigilCoordinator


type VigilConfigEntry = ConfigEntry[VigilEntryData]


async def async_setup(_hass: HomeAssistant, _config: ConfigType) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VigilConfigEntry) -> bool:
    # Deferred imports keep the package importable without the detection stack.
    from .coordinator import VigilCoordinator
    from .learning.interval_learner import IntervalLearner
    from .persistence import IntervalStoreError, create_interval_store

    store = create_interval_store(hass, merged_options(entry.data, entry.options))
    learner = IntervalLearner(hass, store)
    try:
        await learner.async_load()
    except IntervalStoreError as err:
        # Store unreachable: fail setup so HA retries rather than seeding from
        # a false-empty state. Close first so a retried setup doesn't leak the
        # engine/pool the SQLAlchemy backend opened before the load raised.
        try:
            await learner.async_close()
        except Exception:  # cleanup must not mask the load failure
            _LOGGER.exception("Vigil: interval store close after load failure failed")
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = VigilCoordinator(hass, entry, learner)
    # Release learner pool + coordinator resources if any later step raises, so a
    # flapping setup (first_refresh -> ConfigEntryNotReady, which HA retries)
    # doesn't leak on every retry.
    try:
        # Rehydrate state before the first cycle so a restart doesn't re-raise
        # dismissed alerts, reset ongoing faults, or lose an outage start.
        await coordinator.async_load_state()
        await coordinator.async_config_entry_first_refresh()

        entry.runtime_data = VigilEntryData(coordinator=coordinator)

        # The card must not hold detection hostage to a frontend hiccup.
        try:
            await _async_register_frontend(hass)
        except Exception:  # never let the card block detection
            _LOGGER.exception("Vigil: failed to register dashboard card; continuing")

        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Release resources without letting cleanup mask the propagating failure
        # (HA must see ConfigEntryNotReady, not a dispose error).
        coordinator.async_teardown()
        try:
            await learner.async_close()
        except Exception:  # cleanup must not swallow the original error
            _LOGGER.exception("Vigil: interval store close after setup failure failed")
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: VigilConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    coordinator = entry.runtime_data.coordinator
    coordinator.async_teardown()
    # Best-effort cleanup, each step independent. Flush intervals and persist
    # state (bypassing the debounce), THEN close the store last — all writes go
    # through the same backend and the close disposes the pool.
    learner = coordinator.learner
    cleanup_steps: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
        ("interval flush", lambda: learner.async_flush(dt_util.utcnow())),
        *(
            (f"{label} persist", repo.async_persist_now)
            for label, repo in coordinator.state_repos
        ),
        ("interval store close", learner.async_close),
    )
    for label, step in cleanup_steps:
        try:
            await step()
        except Exception:  # cleanup is best-effort; never abort it
            _LOGGER.exception("Vigil: %s on unload failed", label)
    # The static path, HTTP view, and card resource persist for the process
    # lifetime; nothing per-entry to retract here.
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the JSON state view, static module dir, and the Lovelace card.

    View and static path persist for the process lifetime, so each is guarded to
    register at most once.
    """
    from .http_api import VigilStateView

    if not hass.data.get(DATA_VIEW_REGISTERED):
        hass.http.register_view(VigilStateView(hass))
        hass.data[DATA_VIEW_REGISTERED] = True

    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    if not hass.data.get(DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_PATH, frontend_dir, cache_headers=False)]
        )
        hass.data[DATA_STATIC_REGISTERED] = True

    await _async_register_card_resource(hass)


async def _lovelace_resources(
    hass: HomeAssistant,
) -> ResourceStorageCollection | None:
    """The Lovelace storage-mode resource collection, loaded — or None when
    Lovelace is unavailable or in YAML mode (nothing to register/remove there)."""
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    from homeassistant.components.lovelace.resources import ResourceStorageCollection

    resources = getattr(hass.data.get(LOVELACE_DATA), "resources", None)
    if not isinstance(resources, ResourceStorageCollection):
        return None
    if not getattr(resources, "loaded", True):
        await resources.async_load()
    return resources


def _is_card_resource(item: Any) -> bool:
    """Whether a Lovelace resource item is our card (ignoring the ?v= cache-buster)."""
    return str(item.get("url", "")).split("?", 1)[0].endswith(f"/{CARD_FILENAME}")


async def _async_register_card_resource(hass: HomeAssistant) -> None:
    """Register the card JS as a Lovelace resource (storage mode).

    The resource loader awaits the module's custom-element definition before
    rendering, closing the load race that surfaces as a whole-view "Configuration
    error". The URL carries a ``?v=<mtime>`` cache-buster so a redeployed card is
    actually re-fetched: an existing resource is UPDATED when the version changes,
    not left stale. Falls back to a global extra-module URL when Lovelace is in
    YAML mode or unavailable.
    """
    version = await hass.async_add_executor_job(
        _file_version,
        os.path.join(os.path.dirname(__file__), "frontend", CARD_FILENAME),
    )
    url = f"{STATIC_PATH}/{CARD_FILENAME}?v={version}"
    try:
        resources = await _lovelace_resources(hass)
        if resources is not None:
            for item in resources.async_items():
                if _is_card_resource(item):
                    existing = str(item.get("url", ""))
                    # Refresh the cache-buster if the deployed card changed.
                    if existing != url:
                        await resources.async_update_item(
                            item["id"], {"res_type": "module", "url": url}
                        )
                        _LOGGER.debug("Vigil: updated card resource to %s", url)
                    return
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.debug("Vigil: registered card as a Lovelace resource (%s)", url)
            return
    except Exception:  # fall back; never block setup on this
        _LOGGER.exception(
            "Vigil: Lovelace resource registration failed; using extra-module URL"
        )

    # YAML-mode (or lovelace not ready): a global extra module is the only
    # option. extra_js_url has no removal API, so guard to add once.
    if not hass.data.get(DATA_CARD_REGISTERED):
        frontend.add_extra_js_url(hass, url)
        hass.data[DATA_CARD_REGISTERED] = True


async def async_remove_entry(hass: HomeAssistant, _entry: ConfigEntry) -> None:
    """On uninstall, remove the Lovelace resource we created so it doesn't dangle."""
    try:
        resources = await _lovelace_resources(hass)
        if resources is None:
            return
        for item in list(resources.async_items()):
            if _is_card_resource(item):
                await resources.async_delete_item(item["id"])
    except Exception:  # best-effort cleanup
        _LOGGER.debug("Vigil: could not remove card Lovelace resource", exc_info=True)


def _file_version(path: str) -> int:
    """Integer file version (mtime) for cache-busting; 0 if unavailable."""
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0
