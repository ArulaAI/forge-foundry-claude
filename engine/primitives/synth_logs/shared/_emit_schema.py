"""Regenerate the committed M6 JSON Schemas.

Run as::

    uv run python -m primitives.synth_logs.shared._emit_schema

Re-runs ``model_json_schema()`` on each top-level persisted model
(``EntitiesDigest``, ``ScenarioPlan``, ``ValidationReport``,
``ManifestRecord``) and overwrites the committed files under
``primitives/synth_logs/shared/schemas/``. The committed copies exist so
downstream dashboards / consumers can lock against a stable JSON Schema
without importing the Python model (design spec §"File contracts" rule).
``tests/test_synth_logs_models.py::test_committed_schemas_match_models``
fails the build if any static file drifts.

Mirrors the M4-3 ``primitives/sp_converter/shared/_emit_schema.py``
helper. Nested models (``IncidentEvent``, ``LogFileSpec``,
``ValidationRuleResult``, ``GeneratedFileRecord``) appear under
``$defs`` in their respective parent schemas — one canonical surface
per top-level shape, not seven drift-prone copies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from primitives.synth_logs.shared.models import (
    EntitiesDigest,
    ManifestRecord,
    ScenarioPlan,
    ValidationReport,
)

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from pydantic import BaseModel

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

_TOP_LEVEL_MODELS: dict[str, type[BaseModel]] = {
    "entities_digest.json": EntitiesDigest,
    "scenario_plan.json": ScenarioPlan,
    "validation_report.json": ValidationReport,
    "manifest_record.json": ManifestRecord,
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
