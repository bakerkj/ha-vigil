# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""The shared entity selector — a declarative match over a device's entities.

One matcher used by both the Engine-4 watch rules and the ``vigil.yaml`` ignore
rules: an integration (the entity's platform) plus zero or more entity criteria.
Every criterion that is set must match; an unset criterion is a wildcard. Imports
only ``models`` and Home Assistant types, so any layer can depend on it.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.core import State
from homeassistant.helpers.entity_registry import RegistryEntry

from .models import resolved_device_class


@dataclass(frozen=True)
class EntitySelector:
    """A declarative match over a registry entry (integration + entity criteria)."""

    # The entity's platform (e.g. ``litterrobot``); None = any integration.
    integration: str | None = None
    entity_id_glob: str | None = None
    entity_id_suffix: str | None = None
    device_class: str | None = None
    translation_key: str | None = None

    @classmethod
    def from_match(
        cls, integration: str | None, match: Mapping[str, Any]
    ) -> EntitySelector:
        """Build a selector from a rule's ``integration`` + its ``match`` mapping.

        The four ``match`` keys mirror the criteria fields; an absent key is a
        wildcard. Living next to the fields keeps the key list from drifting across
        the watch/ignore rule parsers that both build selectors this way.
        """
        return cls(
            integration=integration,
            entity_id_glob=match.get("entity_id_glob"),
            entity_id_suffix=match.get("entity_id_suffix"),
            device_class=match.get("device_class"),
            translation_key=match.get("translation_key"),
        )

    def matches(self, entry: RegistryEntry, state: State | None) -> bool:
        """Whether ``entry`` (with its live ``state``) satisfies every set criterion."""
        if self.integration is not None and entry.platform != self.integration:
            return False
        eid = entry.entity_id
        if self.entity_id_glob is not None and not fnmatch.fnmatchcase(
            eid, self.entity_id_glob
        ):
            return False
        if self.entity_id_suffix is not None and not eid.endswith(
            self.entity_id_suffix
        ):
            return False
        if (
            self.device_class is not None
            and resolved_device_class(entry, state) != self.device_class
        ):
            return False
        return not (
            self.translation_key is not None
            and entry.translation_key != self.translation_key
        )
