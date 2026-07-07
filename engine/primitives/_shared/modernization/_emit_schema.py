"""Regenerate the committed shared-modernization-engine JSON Schemas.

Run as::

    uv run python -m primitives._shared.modernization._emit_schema

Re-runs ``model_json_schema()`` on each top-level persisted artifact model
(``EngineFacts``, ``ModernizationDigest``, ``SystemMap``, ``Roadmap``) and
overwrites the committed files under
``primitives/_shared/modernization/schemas/``. The committed copies exist so
downstream M3/M4/M5 pipelines (and external consumers) can lock against a stable
JSON Schema without importing the Python model (design spec §2 two-artifact
discipline). ``tests/test_modernization_engine.py::test_committed_schemas_match_models``
fails the build if any static file drifts.

Mirrors ``primitives/synth_logs/shared/_emit_schema.py``. Nested models
(``FactsUnit``, ``FactsInterface``, ``SystemMapNode``/``SystemMapEdge``,
``RoadmapItem``/``RoadmapWave``) appear under ``$defs`` of their top-level parent
— one canonical surface per artifact, not many drift-prone copies. Files use the
``.schema.json`` suffix (#632 acceptance).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from primitives._shared.modernization.models import (
    EngineFacts,
    ModernizationDigest,
    Roadmap,
    SystemMap,
)

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from pydantic import BaseModel

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

_TOP_LEVEL_MODELS: dict[str, type[BaseModel]] = {
    "facts.schema.json": EngineFacts,
    "digest.schema.json": ModernizationDigest,
    "system_map.schema.json": SystemMap,
    "roadmap.schema.json": Roadmap,
}


def emit_schemas(target_dir: Path | None = None) -> list[Path]:
    """Write fresh JSON Schemas for every top-level model.

    Returns the list of paths written, in deterministic name order.
    """
    out_dir = target_dir if target_dir is not None else _SCHEMA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in _TOP_LEVEL_MODELS.items():
        path = out_dir / filename
        schema = model.model_json_schema()
        with path.open("w", encoding="utf-8") as fh:
            json.dump(schema, fh, indent=2, sort_keys=True)
            fh.write("\n")
        written.append(path)
    return written


if __name__ == "__main__":
    paths = emit_schemas()
    # Module is invoked as ``python -m`` — print is the right channel here.
    for p in paths:
        print(f"Wrote {p}")
