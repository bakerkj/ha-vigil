# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Parity guard: the dashboard card must render every backend issue bucket.

Cheap Python-only check (no JS toolchain) so that adding a bucket to
``ISSUE_BUCKETS`` without wiring it into the card fails CI — the exact miss that
shipped the app bucket to the API/sensors but not the card.
"""

from __future__ import annotations

from pathlib import Path

from custom_components.vigil.models import ISSUE_BUCKETS

_CARD = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "vigil"
    / "frontend"
    / "vigil-card.js"
)


def test_card_renders_every_issue_bucket() -> None:
    card = _CARD.read_text(encoding="utf-8")
    missing = [b.key for b in ISSUE_BUCKETS if f'"{b.key}"' not in card]
    assert not missing, f"vigil-card.js does not reference issue bucket(s): {missing}"
