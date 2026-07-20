# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol

from custom_components.vigil.config_flow import (
    _as_list,
    _as_number,
    _build_schema,
)
from custom_components.vigil.const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("90", 90),
        (90.7, 90),
        (None, DEFAULT_SCAN_INTERVAL),
        ("not-a-number", DEFAULT_SCAN_INTERVAL),
        ([], DEFAULT_SCAN_INTERVAL),
    ],
)
def test_as_int_coercion(value: Any, expected: int) -> None:
    assert _as_number(value, DEFAULT_SCAN_INTERVAL, int) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3.5", 3.5),
        (4, 4.0),
        (None, 3.0),
        ("bad", 3.0),
        ({}, 3.0),
    ],
)
def test_as_float_coercion(value: Any, expected: float) -> None:
    assert _as_number(value, 3.0, float) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["a", "b"], ["a", "b"]),
        (None, []),
        ("a", []),
        (5, []),
    ],
)
def test_as_list_coercion(value: Any, expected: list[Any]) -> None:
    assert _as_list(value) == expected


def test_schema_defaults_from_bad_values() -> None:
    """A non-numeric stored value falls back to the default in the schema."""
    schema = _build_schema({CONF_SCAN_INTERVAL: "garbage"})
    validated = schema(
        {
            "grace_period_minutes": 15,
            "staleness_multiplier": 3.0,
            "startup_ignore_seconds": 300,
            "battery_device_grace_multiplier": 2.0,
        }
    )
    assert validated[CONF_SCAN_INTERVAL] == DEFAULT_SCAN_INTERVAL


def test_schema_clamps_out_of_bounds_default() -> None:
    """An out-of-bounds stored value is clamped to the nearest bound for the default."""
    schema = _build_schema({CONF_SCAN_INTERVAL: MAX_SCAN_INTERVAL + 999})
    validated = schema(
        {
            "grace_period_minutes": 15,
            "staleness_multiplier": 3.0,
            "startup_ignore_seconds": 300,
            "battery_device_grace_multiplier": 2.0,
        }
    )
    assert validated[CONF_SCAN_INTERVAL] == MAX_SCAN_INTERVAL


def test_schema_rejects_out_of_bounds_scan_interval() -> None:
    """The NumberSelector enforces the configured min/max bounds."""
    schema = _build_schema({})
    with pytest.raises(vol.Invalid):
        schema(
            {
                CONF_SCAN_INTERVAL: MAX_SCAN_INTERVAL + 1,
                "grace_period_minutes": 15,
                "staleness_multiplier": 3.0,
                "startup_ignore_seconds": 300,
                "battery_device_grace_multiplier": 2.0,
            }
        )
    with pytest.raises(vol.Invalid):
        schema(
            {
                CONF_SCAN_INTERVAL: MIN_SCAN_INTERVAL - 1,
                "grace_period_minutes": 15,
                "staleness_multiplier": 3.0,
                "startup_ignore_seconds": 300,
                "battery_device_grace_multiplier": 2.0,
            }
        )
