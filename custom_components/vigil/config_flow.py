# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AVAILABILITY_IGNORED_PLATFORMS,
    CONF_BATTERY_GRACE_MULTIPLIER,
    CONF_ENABLE_APP_MONITORING,
    CONF_ENABLE_NOTIFICATION,
    CONF_EXCLUDED_DEVICE_IDS,
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITY_IDS,
    CONF_EXCLUDED_APPS,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_GRACE_PERIOD_MINUTES,
    CONF_INTERVAL_STORE_URL,
    CONF_RECORDER_LOOKBACK_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_STALENESS_EXCLUDED_DEVICE_IDS,
    CONF_STALENESS_EXCLUDED_INTEGRATIONS,
    CONF_STALENESS_MULTIPLIER,
    CONF_STARTUP_IGNORE_SECONDS,
    DEFAULT_BATTERY_GRACE_MULTIPLIER,
    DEFAULT_ENABLE_APP_MONITORING,
    DEFAULT_ENABLE_NOTIFICATION,
    DEFAULT_GRACE_PERIOD_MINUTES,
    DEFAULT_INTERVAL_STORE_URL,
    DEFAULT_RECORDER_LOOKBACK_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALENESS_MULTIPLIER,
    DEFAULT_STARTUP_IGNORE_SECONDS,
    DOMAIN,
    MAX_BATTERY_GRACE_MULTIPLIER,
    MAX_GRACE_PERIOD_MINUTES,
    MAX_RECORDER_LOOKBACK_DAYS,
    MAX_SCAN_INTERVAL,
    MAX_STALENESS_MULTIPLIER,
    MAX_STARTUP_IGNORE_SECONDS,
    MIN_BATTERY_GRACE_MULTIPLIER,
    MIN_GRACE_PERIOD_MINUTES,
    MIN_RECORDER_LOOKBACK_DAYS,
    MIN_SCAN_INTERVAL,
    MIN_STALENESS_MULTIPLIER,
    MIN_STARTUP_IGNORE_SECONDS,
    NAME,
    UNIQUE_ID,
    merged_options,
)

# Defaults for the create (user) flow.
DEFAULTS: dict[str, Any] = {
    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    CONF_GRACE_PERIOD_MINUTES: DEFAULT_GRACE_PERIOD_MINUTES,
    CONF_STALENESS_MULTIPLIER: DEFAULT_STALENESS_MULTIPLIER,
    CONF_STARTUP_IGNORE_SECONDS: DEFAULT_STARTUP_IGNORE_SECONDS,
    CONF_BATTERY_GRACE_MULTIPLIER: DEFAULT_BATTERY_GRACE_MULTIPLIER,
    CONF_RECORDER_LOOKBACK_DAYS: DEFAULT_RECORDER_LOOKBACK_DAYS,
    CONF_ENABLE_NOTIFICATION: DEFAULT_ENABLE_NOTIFICATION,
    CONF_ENABLE_APP_MONITORING: DEFAULT_ENABLE_APP_MONITORING,
    CONF_EXCLUDED_DOMAINS: [],
    CONF_EXCLUDED_INTEGRATIONS: [],
    CONF_EXCLUDED_ENTITY_IDS: [],
    CONF_EXCLUDED_DEVICE_IDS: [],
    CONF_STALENESS_EXCLUDED_INTEGRATIONS: [],
    CONF_STALENESS_EXCLUDED_DEVICE_IDS: [],
    CONF_AVAILABILITY_IGNORED_PLATFORMS: [],
    CONF_EXCLUDED_APPS: [],
    CONF_INTERVAL_STORE_URL: DEFAULT_INTERVAL_STORE_URL,
}


def _as_number(value: Any, default: Any, cast: Callable[[Any], Any]) -> Any:
    """Coerce ``value`` via ``cast``, falling back to ``default``.

    Used only for the pre-fill default (a stored value may be non-numeric);
    submitted values are coerced by the schema's ``vol.Coerce``.
    """
    try:
        return cast(value)
    except TypeError, ValueError:
        return default


def _as_list(value: Any) -> list[Any]:
    """Coerce a stored value to a ``list``, falling back to an empty list."""
    return value if isinstance(value, list) else []


# NumberSelector (BOX) fields, data-driven: (key, default, min, max, step,
# unit|None, cast). Adding a numeric knob is one row here.
_NUMERIC_FIELDS: tuple[tuple[Any, ...], ...] = (
    (
        CONF_SCAN_INTERVAL,
        DEFAULT_SCAN_INTERVAL,
        MIN_SCAN_INTERVAL,
        MAX_SCAN_INTERVAL,
        1,
        "s",
        int,
    ),
    (
        CONF_GRACE_PERIOD_MINUTES,
        DEFAULT_GRACE_PERIOD_MINUTES,
        MIN_GRACE_PERIOD_MINUTES,
        MAX_GRACE_PERIOD_MINUTES,
        1,
        "min",
        int,
    ),
    (
        CONF_STALENESS_MULTIPLIER,
        DEFAULT_STALENESS_MULTIPLIER,
        MIN_STALENESS_MULTIPLIER,
        MAX_STALENESS_MULTIPLIER,
        0.5,
        None,
        float,
    ),
    (
        CONF_STARTUP_IGNORE_SECONDS,
        DEFAULT_STARTUP_IGNORE_SECONDS,
        MIN_STARTUP_IGNORE_SECONDS,
        MAX_STARTUP_IGNORE_SECONDS,
        1,
        "s",
        int,
    ),
    (
        CONF_BATTERY_GRACE_MULTIPLIER,
        DEFAULT_BATTERY_GRACE_MULTIPLIER,
        MIN_BATTERY_GRACE_MULTIPLIER,
        MAX_BATTERY_GRACE_MULTIPLIER,
        0.5,
        None,
        float,
    ),
    (
        CONF_RECORDER_LOOKBACK_DAYS,
        DEFAULT_RECORDER_LOOKBACK_DAYS,
        MIN_RECORDER_LOOKBACK_DAYS,
        MAX_RECORDER_LOOKBACK_DAYS,
        1,
        "d",
        int,
    ),
)

# Multi-select list fields: (key, kind in {"text","entity","device"}); [] default.
_LIST_FIELDS: tuple[tuple[str, str], ...] = (
    (CONF_EXCLUDED_DOMAINS, "text"),
    (CONF_EXCLUDED_INTEGRATIONS, "text"),
    (CONF_EXCLUDED_ENTITY_IDS, "entity"),
    (CONF_EXCLUDED_DEVICE_IDS, "device"),
    (CONF_STALENESS_EXCLUDED_INTEGRATIONS, "text"),
    (CONF_STALENESS_EXCLUDED_DEVICE_IDS, "device"),
    (CONF_AVAILABILITY_IGNORED_PLATFORMS, "text"),
    (CONF_EXCLUDED_APPS, "text"),
)

# Boolean (toggle) fields: (key, default).
_BOOL_FIELDS: tuple[tuple[str, bool], ...] = (
    (CONF_ENABLE_NOTIFICATION, DEFAULT_ENABLE_NOTIFICATION),
    (CONF_ENABLE_APP_MONITORING, DEFAULT_ENABLE_APP_MONITORING),
)


def _list_selector(kind: str) -> Any:
    """A multiple-select selector of the given kind (default: free-text tags)."""
    if kind == "entity":
        return selector.EntitySelector(selector.EntitySelectorConfig(multiple=True))
    if kind == "device":
        return selector.DeviceSelector(selector.DeviceSelectorConfig(multiple=True))
    return selector.TextSelector(selector.TextSelectorConfig(multiple=True))


def _build_schema(values: Mapping[str, Any]) -> vol.Schema:
    """Build the shared config/options schema, defaulting from ``values``.

    Fields are data-driven from ``_NUMERIC_FIELDS`` / ``_BOOL_FIELDS`` /
    ``_LIST_FIELDS``.
    """
    schema: dict[Any, Any] = {}
    for key, default, lo, hi, step, unit, cast in _NUMERIC_FIELDS:
        cfg = selector.NumberSelectorConfig(
            min=lo, max=hi, step=step, mode=selector.NumberSelectorMode.BOX
        )
        if unit is not None:
            cfg["unit_of_measurement"] = unit
        # Clamp the pre-filled default: a stored value outside the current bounds
        # would render as an out-of-range default the form rejects on submit.
        default_value = vol.Clamp(min=lo, max=hi)(
            _as_number(values.get(key), default, cast)
        )
        # NumberSelector returns a float even for an int knob; vol.Coerce casts
        # it back to the declared type.
        schema[vol.Required(key, default=default_value)] = vol.All(
            selector.NumberSelector(cfg), vol.Coerce(cast)
        )

    for key, bool_default in _BOOL_FIELDS:
        schema[vol.Required(key, default=bool(values.get(key, bool_default)))] = (
            selector.BooleanSelector()
        )

    for key, kind in _LIST_FIELDS:
        schema[vol.Optional(key, default=_as_list(values.get(key)))] = _list_selector(
            kind
        )

    # Optional external-DB URL for the interval store (blank = local SQLite).
    # Masked (password field) since the URL embeds DB credentials.
    store_url = values.get(CONF_INTERVAL_STORE_URL, DEFAULT_INTERVAL_STORE_URL)
    schema[
        vol.Optional(
            CONF_INTERVAL_STORE_URL,
            default=str(store_url) if store_url else "",
        )
    ] = selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )

    return vol.Schema(schema)


class VigilConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial (UI) setup of the single Vigil instance."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the setup form and create the single config entry."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            await self.async_set_unique_id(UNIQUE_ID)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=NAME, data=user_input)

        return self.async_show_form(step_id="user", data_schema=_build_schema(DEFAULTS))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return VigilOptionsFlow()


class VigilOptionsFlow(OptionsFlow):
    """Handle re-configuration of an existing Vigil entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form and persist any changes."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        values = merged_options(self.config_entry.data, self.config_entry.options)
        return self.async_show_form(step_id="init", data_schema=_build_schema(values))
