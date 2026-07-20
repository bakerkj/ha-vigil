# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.core import State

from custom_components.vigil.selectors import EntitySelector

LR = EntitySelector(
    integration="litterrobot",
    device_class="connectivity",
    entity_id_suffix="_hopper_connected",
)


@pytest.mark.parametrize(
    (
        "selector",
        "entity_id",
        "platform",
        "device_class",
        "translation_key",
        "expected",
    ),
    [
        # An empty selector is a wildcard — matches anything.
        (EntitySelector(), "binary_sensor.x", "demo", None, None, True),
        # Integration (the entity's platform).
        (
            EntitySelector(integration="demo"),
            "binary_sensor.x",
            "demo",
            None,
            None,
            True,
        ),
        (
            EntitySelector(integration="other"),
            "binary_sensor.x",
            "demo",
            None,
            None,
            False,
        ),
        # Glob and suffix on the entity id.
        (
            EntitySelector(entity_id_glob="*_hopper"),
            "binary_sensor.a_hopper",
            "d",
            None,
            None,
            True,
        ),
        (
            EntitySelector(entity_id_glob="*_hopper"),
            "binary_sensor.a_online",
            "d",
            None,
            None,
            False,
        ),
        (
            EntitySelector(entity_id_suffix="_hopper_connected"),
            "binary_sensor.r_hopper_connected",
            "d",
            None,
            None,
            True,
        ),
        (
            EntitySelector(entity_id_suffix="_hopper_connected"),
            "binary_sensor.r_online",
            "d",
            None,
            None,
            False,
        ),
        # Device class (resolved) and translation key.
        (
            EntitySelector(device_class="connectivity"),
            "binary_sensor.x",
            "d",
            "connectivity",
            None,
            True,
        ),
        (
            EntitySelector(device_class="connectivity"),
            "binary_sensor.x",
            "d",
            "problem",
            None,
            False,
        ),
        (
            EntitySelector(translation_key="hopper"),
            "binary_sensor.x",
            "d",
            None,
            "hopper",
            True,
        ),
        (
            EntitySelector(translation_key="hopper"),
            "binary_sensor.x",
            "d",
            None,
            "other",
            False,
        ),
        # The Litter-Robot case: every set criterion must match.
        (
            LR,
            "binary_sensor.litter_robot_5_pro_hopper_connected",
            "litterrobot",
            "connectivity",
            None,
            True,
        ),
        (
            LR,
            "binary_sensor.litter_robot_5_pro_online",
            "litterrobot",
            "connectivity",
            None,
            False,
        ),  # suffix
        (
            LR,
            "binary_sensor.litter_robot_5_pro_hopper_connected",
            "other",
            "connectivity",
            None,
            False,
        ),  # integration
    ],
)
def test_entity_selector_matches(
    selector: EntitySelector,
    entity_id: str,
    platform: str,
    device_class: str | None,
    translation_key: str | None,
    expected: bool,
) -> None:
    entry = SimpleNamespace(
        entity_id=entity_id,
        platform=platform,
        device_class=device_class,
        original_device_class=None,
        translation_key=translation_key,
    )
    assert selector.matches(entry, State(entity_id, "on")) is expected  # type: ignore[arg-type]
