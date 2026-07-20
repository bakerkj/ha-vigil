# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.vigil as vigil_init
from custom_components.vigil import (
    DATA_CARD_REGISTERED,
    DATA_STATIC_REGISTERED,
    DATA_VIEW_REGISTERED,
    VigilEntryData,
    _async_register_card_resource,
    _async_register_frontend,
)
from custom_components.vigil.config_flow import DEFAULTS
from custom_components.vigil.const import (
    CARD_FILENAME,
    DOMAIN,
    NOTIFICATION_ID,
    STATIC_PATH,
)
from custom_components.vigil.sensor import async_setup_entry as sensor_async_setup_entry


def _wire_setup(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> list[Entity]:
    """Stub the frontend + platform forwarding and drive the sensor platform directly."""
    added: list[Entity] = []

    async def _noop_frontend(_hass: HomeAssistant) -> None:
        return None

    async def _fake_forward(entry: ConfigEntry, platforms: list[Platform]) -> None:
        assert platforms == [Platform.SENSOR, Platform.BUTTON]

        def _collect(new: Iterable[Entity], _update: bool = False) -> None:
            added.extend(new)

        await sensor_async_setup_entry(hass, entry, _collect)  # type: ignore[arg-type]

    async def _fake_unload(entry: ConfigEntry, platforms: list[Platform]) -> bool:
        return True

    monkeypatch.setattr(vigil_init, "_async_register_frontend", _noop_frontend)
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", _fake_forward
    )
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", _fake_unload)
    return added


def _add_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Vigil", data=dict(DEFAULTS))
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return entry


async def test_setup_entry_populates_data_and_entities(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """async_setup_entry creates the coordinator data and the six sensors."""
    added = _wire_setup(hass, monkeypatch)
    entry = _add_entry(hass)

    assert await vigil_init.async_setup_entry(hass, entry) is True
    await hass.async_block_till_done()

    entry_data = entry.runtime_data
    assert isinstance(entry_data, VigilEntryData)
    assert entry_data.coordinator.data is not None
    assert len(added) == 7


class _FailingLoadStore:
    """A store whose load fails — stands in for an unreachable external DB."""

    def __init__(self) -> None:
        self.closed = False

    async def async_load(self) -> object:
        from custom_components.vigil.persistence import IntervalStoreError

        raise IntervalStoreError("interval store load failed")

    async def async_flush(self, changes: object) -> bool:
        return True

    async def async_close(self) -> None:
        self.closed = True


async def test_setup_entry_raises_not_ready_on_store_load_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store load failure fails setup with ConfigEntryNotReady so HA retries —
    and the store is closed first so a retried setup doesn't leak its pool."""
    from homeassistant.exceptions import ConfigEntryNotReady

    import custom_components.vigil.persistence as persistence_mod

    store = _FailingLoadStore()
    monkeypatch.setattr(persistence_mod, "create_interval_store", lambda *a, **k: store)
    entry = _add_entry(hass)

    with pytest.raises(ConfigEntryNotReady):
        await vigil_init.async_setup_entry(hass, entry)
    assert store.closed is True, "store must be closed on the load-failure path"


class _ClosableStore:
    """A store that loads/flushes fine but records async_close()."""

    def __init__(self) -> None:
        self.closed = False

    async def async_load(self) -> object:
        from custom_components.vigil.persistence.interval_store import LoadedState

        return LoadedState()

    async def async_flush(self, changes: object) -> bool:
        return True

    async def async_load_state(self, key: str) -> object | None:
        return None

    async def async_save_state(self, key: str, value: object) -> bool:
        return True

    async def async_close(self) -> None:
        self.closed = True


async def test_setup_entry_closes_store_when_a_later_step_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setup step failing after the store opened must still close the store."""
    from homeassistant.exceptions import ConfigEntryNotReady

    import custom_components.vigil.persistence as persistence_mod
    from custom_components.vigil.coordinator import VigilCoordinator

    store = _ClosableStore()
    monkeypatch.setattr(persistence_mod, "create_interval_store", lambda *a, **k: store)

    async def _boom(self: VigilCoordinator) -> None:
        raise ConfigEntryNotReady("first refresh failed")

    monkeypatch.setattr(VigilCoordinator, "async_config_entry_first_refresh", _boom)
    _wire_setup(hass, monkeypatch)
    entry = _add_entry(hass)

    with pytest.raises(ConfigEntryNotReady):
        await vigil_init.async_setup_entry(hass, entry)
    assert store.closed is True, "the interval store must be closed on setup failure"


async def test_setup_failure_close_error_does_not_mask_original(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close error during setup-failure cleanup must not mask the original exception."""
    from homeassistant.exceptions import ConfigEntryNotReady

    import custom_components.vigil.persistence as persistence_mod
    from custom_components.vigil.coordinator import VigilCoordinator

    class _CloseFailsStore(_ClosableStore):
        close_attempted = False

        async def async_close(self) -> None:
            type(self).close_attempted = True
            raise RuntimeError("dispose boom")

    monkeypatch.setattr(
        persistence_mod, "create_interval_store", lambda *a, **k: _CloseFailsStore()
    )

    async def _boom(self: VigilCoordinator) -> None:
        raise ConfigEntryNotReady("first refresh failed")

    monkeypatch.setattr(VigilCoordinator, "async_config_entry_first_refresh", _boom)
    _wire_setup(hass, monkeypatch)
    entry = _add_entry(hass)

    with pytest.raises(ConfigEntryNotReady):
        await vigil_init.async_setup_entry(hass, entry)
    # Close was attempted (its error was swallowed, not skipped).
    assert _CloseFailsStore.close_attempted is True


async def test_unload_still_closes_and_persists_when_flush_raises(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flush that raises during unload must not skip close + persist-now calls."""
    _wire_setup(hass, monkeypatch)
    entry = _add_entry(hass)
    assert await vigil_init.async_setup_entry(hass, entry) is True
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    calls: list[str] = []

    async def _boom_flush(now: object) -> None:
        raise RuntimeError("flush boom")

    async def _track_close() -> None:
        calls.append("close")

    monkeypatch.setattr(coordinator.learner, "async_flush", _boom_flush)
    monkeypatch.setattr(coordinator.learner, "async_close", _track_close)
    # Each repo's persist records its label, in the order the unload loop runs them.
    for label, repo in coordinator.state_repos:

        async def _track(_label: str = label) -> None:
            calls.append(_label)

        monkeypatch.setattr(repo, "async_persist_now", _track)

    assert await vigil_init.async_unload_entry(hass, entry) is True
    await hass.async_block_till_done()

    # Flush raised, but every persist still ran, and the store close is LAST (all
    # state writes go through the same backend the close disposes).
    assert calls == ["acknowledged", "fault", "downtime", "app-health", "close"]


async def test_unload_entry_cleans_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """async_unload_entry pops data, dismisses notification, flushes learner."""
    _wire_setup(hass, monkeypatch)
    entry = _add_entry(hass)
    assert await vigil_init.async_setup_entry(hass, entry) is True
    await hass.async_block_till_done()

    # Track that the learner is flushed on unload.
    coordinator = entry.runtime_data.coordinator
    flushed: list[bool] = []

    async def _track_flush(now: object) -> None:
        flushed.append(True)

    monkeypatch.setattr(coordinator.learner, "async_flush", _track_flush)

    # Seed a live notification so we can confirm teardown dismisses it.
    persistent_notification.async_create(
        hass, "x", title="Vigil", notification_id=NOTIFICATION_ID
    )

    assert await vigil_init.async_unload_entry(hass, entry) is True
    await hass.async_block_till_done()

    # Single-instance: the whole DOMAIN bucket is removed once the last entry goes.
    assert DOMAIN not in hass.data
    assert flushed == [True]
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert NOTIFICATION_ID not in notifications


# --- _async_register_frontend register-once guards ---------------------------


class _RecordingHttp:
    """Stand-in for ``hass.http`` recording registration calls."""

    def __init__(self) -> None:
        self.views: list[Any] = []
        self.static_calls: list[Any] = []

    def register_view(self, view: Any) -> None:
        self.views.append(view)

    async def async_register_static_paths(self, configs: Any) -> None:
        self.static_calls.append(configs)


async def test_register_frontend_registers_card_resource_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First call registers the view/static/cache-busted card JS; a second is a no-op."""
    http = _RecordingHttp()
    monkeypatch.setattr(hass, "http", http, raising=False)

    card_urls: list[str] = []

    def _fake_add_extra_js_url(
        _hass: HomeAssistant, url: str, es5: bool = False
    ) -> None:
        card_urls.append(url)

    monkeypatch.setattr(
        "homeassistant.components.frontend.add_extra_js_url",
        _fake_add_extra_js_url,
    )
    hass.data.pop(DATA_VIEW_REGISTERED, None)
    hass.data.pop(DATA_STATIC_REGISTERED, None)
    hass.data.pop(DATA_CARD_REGISTERED, None)

    await _async_register_frontend(hass)
    assert len(http.views) == 1
    assert len(http.static_calls) == 1
    assert len(card_urls) == 1
    assert card_urls[0].startswith(f"{STATIC_PATH}/{CARD_FILENAME}?v=")
    assert hass.data[DATA_VIEW_REGISTERED] is True
    assert hass.data[DATA_STATIC_REGISTERED] is True
    assert hass.data[DATA_CARD_REGISTERED] is True

    # Second invocation: all three guards short-circuit.
    await _async_register_frontend(hass)
    assert len(http.views) == 1
    assert len(http.static_calls) == 1
    assert len(card_urls) == 1


async def test_register_card_falls_back_to_extra_js_on_resource_error(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Lovelace resource registration raises, setup must not blow up: it
    falls back to the global extra-module URL and still marks the card done."""
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    card_urls: list[str] = []
    monkeypatch.setattr(
        "homeassistant.components.frontend.add_extra_js_url",
        lambda _h, url, es5=False: card_urls.append(url),
    )

    class _Boom:
        @property
        def resources(self) -> object:
            raise RuntimeError("lovelace not ready")

    hass.data[LOVELACE_DATA] = _Boom()  # type: ignore[misc]  # only .resources is read
    hass.data.pop(DATA_CARD_REGISTERED, None)

    await _async_register_card_resource(hass)

    assert len(card_urls) == 1
    assert card_urls[0].startswith(f"{STATIC_PATH}/{CARD_FILENAME}?v=")
    assert hass.data[DATA_CARD_REGISTERED] is True
