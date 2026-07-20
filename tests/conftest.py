# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Shared pytest fixtures.

The pytest-homeassistant-custom-component plugin provides the ``hass`` fixture
and other HA-specific helpers. We re-export ``enable_custom_integrations`` here
so individual test modules don't have to import it.
"""

from __future__ import annotations

import contextlib
import logging
import os

import pytest

from custom_components.vigil.const import (
    STORAGE_SQLITE_FILE,
    VIGIL_CONFIG_FILE,
    WATCH_RULES_FILE,
)

pytest_plugins = ["pytest_homeassistant_custom_component"]


def pytest_configure(config: pytest.Config) -> None:
    """Quiet SQLAlchemy's per-statement INFO logging for the suite.

    HA's test plugin raises the ``sqlalchemy.engine`` logger to INFO and installs a
    root stderr handler, which otherwise floods stdout with every BEGIN/INSERT/
    COMMIT from Vigil's interval store (``-p no:logging`` can't suppress it — the
    noise is the plugin's own handler, not pytest's log capture). This hook runs
    after that plugin registers, so it wins. Test behavior is unchanged — only the
    log threshold moves.
    """
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):  # type: ignore[no-untyped-def]
    """Make custom_components importable in every test.

    ``recorder_db_url`` is listed first (it must initialize before the ``hass``
    fixture, which ``enable_custom_integrations`` pulls in) so that recorder-based
    tests can use the ``recorder_mock`` fixture despite this autouse fixture.
    """
    yield


@pytest.fixture(autouse=True)
def _clean_interval_store(
    request: pytest.FixtureRequest,
    auto_enable_custom_integrations: None,
) -> None:
    """Give each test a fresh interval SQLite store.

    Unlike HA's ``Store`` (which the test harness redirects to per-test memory),
    the interval learner writes a real sqlite file under a config dir that is
    stable across the run, so without this its rows would leak between tests. Only
    tests that actually use ``hass`` need cleaning, so hass isn't forced on the
    pure-function tests that don't request it. Depending on
    ``auto_enable_custom_integrations`` keeps ``recorder_db_url`` initializing
    before ``hass`` is pulled in.
    """
    if "hass" not in request.fixturenames:
        return
    hass = request.getfixturevalue("hass")
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(hass.config.path(".storage", STORAGE_SQLITE_FILE + suffix))
    # The config dir is stable across the run, so a vigil.yaml / vigil_watch.yaml a
    # test writes would otherwise leak into every later test.
    for name in (VIGIL_CONFIG_FILE, WATCH_RULES_FILE):
        with contextlib.suppress(FileNotFoundError):
            os.remove(hass.config.path(name))


@pytest.fixture
def recorder_config() -> dict[str, object]:
    """Pin the test recorder's retention to 7 days.

    Vigil's downtime lookback now AUTO-derives from the recorder's configured
    ``purge_keep_days`` (read live off the recorder instance). Pinning it here keeps
    the recorder-reconstruction tests deterministic — a 7-day window, matching how
    they build their histories — instead of depending on the HA default (10).
    ``auto_purge`` is off so a short window never actually purges the historical
    rows a test set up. Tests that need a different window parametrize
    ``recorder_config`` themselves, which overrides this.
    """
    return {"purge_keep_days": 7, "auto_purge": False}
