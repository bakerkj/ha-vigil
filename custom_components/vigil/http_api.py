# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.util import dt as dt_util

from . import VigilEntryData
from .const import API_STATE_PATH, DOMAIN
from .models import empty_vigil_data
from .reporting.serialize import serialize_vigil_data


class VigilStateView(HomeAssistantView):
    """Serve the current Vigil state as JSON for the dashboard card."""

    url = API_STATE_PATH
    name = "api:vigil:state"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Return the serialized Vigil state.

        Before the first cycle produces a payload, return the serialized empty
        payload (same key set) so the card renders the same structure either way.
        """
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            entry_data = getattr(entry, "runtime_data", None)
            if isinstance(entry_data, VigilEntryData):
                data = entry_data.coordinator.data
                if data is None:
                    break
                return self.json(serialize_vigil_data(data))
        return self.json(serialize_vigil_data(empty_vigil_data(dt_util.utcnow())))
