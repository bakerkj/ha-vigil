# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Architecture guard — enforces downward-only dependency tiers.

    tier 0  models, const, storage, selectors   leaves (selectors -> models)
    tier 1  persistence, history, detection/inputs, learning
    tier 2  detection/engines, detection/suppression, reporting/*
    tier 3  pipeline, context
    tier 4  coordinator, http_api, sensor/button/entity/diagnostics/config_flow, __init__

Every module maps to a tier and may only import a tier no higher than its own.
The remaining tests pin same-tier rules ordering can't express (reporting must
not reach into detection; models/const leaves stay pure).
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "vigil"
PKG_PREFIX = "custom_components.vigil"

# Subsystem packages that must never import the coordinator (they sit below it).
SUBSYSTEMS = {"detection", "learning", "history", "reporting", "persistence"}


def _module_name(path: Path) -> str:
    """Dotted module name for a .py file under the package (``__init__`` -> package)."""
    rel = path.relative_to(PKG_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([PKG_PREFIX, *parts]) if parts else PKG_PREFIX


def _package_parts(path: Path) -> list[str]:
    """The dotted parts of the package that CONTAINS ``path``.

    For a package ``__init__.py`` that is the package itself; for a regular module
    it is the module's parent package. This is the base a relative import climbs
    from (``level == 1`` targets this package).
    """
    parts = _module_name(path).split(".")
    return parts if path.name == "__init__.py" else parts[:-1]


def _resolve(pkg_parts: list[str], node: ast.ImportFrom) -> str | None:
    """Resolve an ``ImportFrom`` to the absolute dotted module it targets.

    ``pkg_parts`` is the containing package (see :func:`_package_parts`); ``level``
    1 targets that package, each extra dot climbs one more.
    """
    if node.level == 0:
        return node.module
    climb = node.level - 1
    base = pkg_parts[: len(pkg_parts) - climb] if climb <= len(pkg_parts) else []
    tail = [node.module] if node.module else []
    return ".".join([*base, *tail]) if base else ".".join(tail)


def _subpackage(dotted: str | None) -> str | None:
    """The first path component under ``custom_components.vigil`` (or None)."""
    if not dotted or not dotted.startswith(PKG_PREFIX):
        return None
    rest = dotted[len(PKG_PREFIX) :].lstrip(".")
    return rest.split(".")[0] if rest else ""  # "" == the package root __init__


def _internal_targets(path: Path) -> list[str]:
    """Every intra-package subpackage this module imports (may include ""/None-free)."""
    pkg = _package_parts(path)
    tree = ast.parse(path.read_text(), filename=str(path))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            sub = _subpackage(_resolve(pkg, node))
            if sub is not None:
                targets.append(sub)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                sub = _subpackage(alias.name)
                if sub is not None:
                    targets.append(sub)
    return targets


def _internal_module_targets(path: Path) -> list[str]:
    """Every intra-package MODULE (full dotted, PKG_PREFIX-relative) this imports."""
    pkg = _package_parts(path)
    tree = ast.parse(path.read_text(), filename=str(path))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            dotted = _resolve(pkg, node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PKG_PREFIX):
                    targets.append(alias.name)
            continue
        else:
            continue
        if dotted and dotted.startswith(PKG_PREFIX):
            targets.append(dotted)
    return targets


def _tier_of(dotted: str) -> int:
    """The dependency tier for a PKG_PREFIX-relative dotted module (or the file's
    dotted name). Lower = closer to the leaves; imports may only target <= own tier.
    """
    rest = dotted[len(PKG_PREFIX) :].lstrip(".")
    name = rest.replace(".", "/")  # e.g. "detection/inputs", "" == package root
    if name in ("models", "const", "storage", "selectors"):  # leaves
        return 0
    first = name.split("/")[0]
    if name == "detection/inputs":
        return 1
    if first in ("persistence", "history", "learning"):
        return 1
    if first in ("detection", "reporting"):  # engines/*, suppression, reporting/*
        return 2
    if name in ("pipeline", "context"):
        return 3
    # tier 4 — HA wiring at the package root (and the root __init__, name == "").
    if name in (
        "",
        "coordinator",
        "http_api",
        "sensor",
        "button",
        "entity",
        "diagnostics",
        "config_flow",
    ):
        return 4
    # Fail closed: an unclassified module must be tiered explicitly, so a new
    # package can't silently inherit tier-4 import freedom with a green build.
    raise AssertionError(
        f"module {dotted!r} is not classified in _tier_of; add it to a tier"
    )


def test_no_upward_tier_imports() -> None:
    """Every module may import only modules in a tier no higher than its own."""
    offenders: list[str] = []
    for path in _all_modules():
        own_dotted = _module_name(path)
        own_tier = _tier_of(own_dotted)
        for target in _internal_module_targets(path):
            if _tier_of(target) > own_tier:
                rel = path.relative_to(PKG_ROOT)
                offenders.append(
                    f"{rel} (tier {own_tier}) imports {target} (tier {_tier_of(target)})"
                )
    assert not offenders, "upward tier imports found:\n  " + "\n  ".join(offenders)


def _all_modules() -> list[Path]:
    return sorted(p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _own_subpackage(path: Path) -> str:
    return path.relative_to(PKG_ROOT).parts[0].removesuffix(".py")


def test_subsystems_never_import_the_coordinator() -> None:
    """No detection/learning/history/reporting/persistence/pipeline module may
    import the coordinator — the coordinator sits on top of them."""
    offenders: list[str] = []
    for path in _all_modules():
        own = _own_subpackage(path)
        if own in SUBSYSTEMS or own == "pipeline":
            if "coordinator" in _internal_targets(path):
                offenders.append(str(path.relative_to(PKG_ROOT)))
    assert not offenders, f"these import the coordinator but must not: {offenders}"


def test_reporting_does_not_import_detection() -> None:
    """reporting/ surfaces results; it must not reach into detection/ (the
    ExclusionConfig it needs lives in the models leaf)."""
    offenders: list[str] = []
    for path in _all_modules():
        if _own_subpackage(path) == "reporting":
            if "detection" in _internal_targets(path):
                offenders.append(str(path.relative_to(PKG_ROOT)))
    assert not offenders, f"reporting must not import detection: {offenders}"


def test_leaves_have_no_forbidden_internal_imports() -> None:
    """models may import only const; const and storage import nothing internal."""
    models = PKG_ROOT / "models.py"
    const = PKG_ROOT / "const.py"
    storage = PKG_ROOT / "storage.py"
    models_targets = {t for t in _internal_targets(models) if t not in ("", "const")}
    assert not models_targets, f"models.py must import only const, not {models_targets}"
    const_targets = {t for t in _internal_targets(const) if t != ""}
    assert not const_targets, (
        f"const.py must have no internal imports, not {const_targets}"
    )
    storage_targets = {t for t in _internal_targets(storage) if t != ""}
    assert not storage_targets, (
        f"storage.py must have no internal imports, not {storage_targets}"
    )
