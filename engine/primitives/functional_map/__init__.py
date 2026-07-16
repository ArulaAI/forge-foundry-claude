"""S61 functional-map primitive.

Public surface consumed by the polyglot discovery flavor packs
(`primitives/_shared/discovery/flavor/web_*.py`) and the app-mod scope synthesizer
(`primitives/modernization_scope/shared/pipeline.py`, SP-A slicing). Consumers must
NOT reach into `functional_map.shared.*` directly (QUAL-2, #163); they call this
package surface, which re-exports the report model + the SP-A per-context slicing
helpers so the scope pipeline can obtain per-bounded-context slices.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# Re-export the report model + SP-A slicing helpers so cross-primitive consumers
# (the app-mod scope pipeline) import them from this public surface, not `shared/`.
from primitives.functional_map.shared.models import FunctionalMapReport
from primitives.functional_map.shared.slicing import (
    contexts_from_system_map,
    fallback_contexts,
    slice_functional_map,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "FunctionalMapReport",
    "contexts_from_system_map",
    "fallback_contexts",
    "run_sub_extractors",
    "slice_functional_map",
]

_SUB_EXTRACTOR_PKG = "primitives.functional_map.shared.sub_extractors"


def run_sub_extractors(
    names: tuple[str, ...] | list[str],
    file_path: Path,
    content: str,
) -> dict[str, list[object]]:
    """Run the named sub-extractors over one file and merge their results.

    ``names`` are sub-extractor module names (e.g. ``"angular_routes"``,
    ``"spring_mvc"``). Each module exposes ``parse(file_path, content) -> dict[str, list]``;
    the per-key lists are concatenated in ``names`` order. A missing module is skipped
    so a flavor can list optional extractors without guarding each import.
    """
    results: dict[str, list[object]] = {}
    for name in names:
        try:
            module = importlib.import_module(f"{_SUB_EXTRACTOR_PKG}.{name}")
        except ImportError:
            continue
        for key, val in module.parse(file_path, content).items():
            results.setdefault(key, []).extend(val)
    return results
