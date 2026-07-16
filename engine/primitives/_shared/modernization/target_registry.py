"""Offline target-stack registry lookup (SP1, foundation).

Loads the bundled, checked-in :data:`target_registry.yaml` and resolves a
dimension/value to its latest-known version + canonical label. This is the ONLY
source of "latest version" resolution — there is no network lookup, ever (hard
invariant). The YAML is loaded once and module-cached.

Design: ``docs/superpowers/specs/2026-07-10-target-stack-modernization-design.md`` §1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REGISTRY_PATH = Path(__file__).resolve().parent / "target_registry.yaml"
# Module-level cache: a single-slot mutable container avoids the `global` statement.
_registry_cache: dict[str, dict[str, dict[str, dict[str, str]]]] = {}


def _load_registry() -> dict[str, dict[str, dict[str, str]]]:
    """Load + module-cache the bundled registry YAML (offline, no network)."""
    if "value" not in _registry_cache:
        raw: Any = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
        _registry_cache["value"] = raw
    return _registry_cache["value"]


def latest_known(dimension: str, value: str) -> tuple[str, str]:
    """Resolve ``(version, label)`` for a dimension/value from the bundled registry.

    Returns the latest-known version and its label (with the ``{v}`` placeholder
    substituted with that version). Returns ``("", "")`` when the dimension or
    value is not registered — the caller carries the value verbatim and warns.
    """
    entry = _load_registry().get(dimension, {}).get(value)
    if not entry:
        return "", ""
    version = str(entry.get("latest", ""))
    label = str(entry.get("label", "")).format(v=version)
    return version, label
