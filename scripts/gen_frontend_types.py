# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Generate the frontend API type declarations from the backend wire types.

Standard two-step pipeline, no bespoke type-walking:

    pydantic  TypeAdapter(VigilStateDict).json_schema()   ->  JSON Schema
    json-schema-to-typescript (npm)                        ->  .d.ts

``VigilStateDict`` (models.py) is the single source of truth: mypy already
verifies ``serialize_vigil_data`` / ``VigilIssue.as_dict`` produce exactly that
shape, so the generated ``.d.ts`` can never disagree with what the API serves.

Run with ``npm run gen:types`` (i.e. ``uv run python scripts/gen_frontend_types.py``).
The prek hook and the gen-types CI workflow regenerate and fail on drift.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `custom_components` imports when run as a script

from custom_components.vigil.models import VigilStateDict

_OUT = _ROOT / "custom_components/vigil/frontend/vigil-api.generated.d.ts"
_JSON2TS = _ROOT / "node_modules/.bin/json2ts"

# Backend TypedDict name -> frontend-facing interface name.
_RENAME = {"VigilStateDict": "VigilData", "VigilIssueDict": "VigilIssue"}

_BANNER = """/* eslint-disable */
// Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
// All rights reserved.
//
// GENERATED from the backend wire types (models.VigilStateDict) by
// scripts/gen_frontend_types.py. Do NOT edit by hand -- run `npm run gen:types`."""


def _normalize(schema: dict[str, Any]) -> None:
    """Normalize one JSON Schema node in place, recursing only through structural
    keywords — never through ``properties`` *keys*, so a field literally named
    ``title`` survives.

    - drop the ``title`` *annotation* (json2ts would emit junk aliases from it),
    - forbid extra properties (so json2ts omits ``[k: string]: unknown`` sigs).
    """
    schema.pop("title", None)
    if schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
    for prop in schema.get("properties", {}).values():
        _normalize(prop)
    if isinstance(schema.get("items"), dict):
        _normalize(schema["items"])
    for sub in schema.get("anyOf", []):
        _normalize(sub)
    if isinstance(schema.get("additionalProperties"), dict):
        _normalize(schema["additionalProperties"])


def _build_schema() -> dict[str, Any]:
    schema = TypeAdapter(VigilStateDict).json_schema(
        ref_template="#/definitions/{model}"
    )
    defs = schema.pop("$defs", {})
    # Rename the *Dict backend names to the frontend interface names, refs first.
    blob = json.dumps({"root": schema, "defs": defs})
    for old, new in _RENAME.items():
        blob = blob.replace(f"#/definitions/{old}", f"#/definitions/{new}")
    obj = json.loads(blob)
    schema, defs = obj["root"], obj["defs"]
    for old, new in _RENAME.items():
        if old in defs:
            defs[new] = defs.pop(old)

    _normalize(schema)
    for definition in defs.values():
        _normalize(definition)
    schema["title"] = _RENAME["VigilStateDict"]
    schema["definitions"] = defs
    return schema


def main() -> None:
    schema = _build_schema()
    with tempfile.NamedTemporaryFile(
        "w", suffix=".schema.json", delete=False
    ) as handle:
        json.dump(schema, handle)
        schema_path = handle.name
    try:
        result = subprocess.run(
            [str(_JSON2TS), schema_path, "--bannerComment", _BANNER],
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        Path(schema_path).unlink(missing_ok=True)
    _OUT.write_text(result.stdout)
    print(f"wrote {_OUT.relative_to(_ROOT)}")


if __name__ == "__main__":
    main()
