"""Regenerate the committed ``SchemaDigest`` JSON Schema.

Run as::

    uv run python -m primitives.db_schema_analyze.shared._emit_schema

Re-runs ``SchemaDigest.model_json_schema()`` and overwrites
``primitives/db_schema_analyze/shared/schemas/schema_digest.json``.
The committed copy exists so downstream dashboards / consumers can lock
against a stable JSON Schema without importing the Python model
(addresses the design spec §"JSON schemas emitted" rule + PROMPT-5
carry-forward). ``tests/test_db_schema_analyze_models.py::
test_committed_schema_matches_model`` fails the build if the static
file drifts.
"""

from __future__ import annotations

import json
from pathlib import Path

from primitives.db_schema_analyze.shared.models import SchemaDigest

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "schema_digest.json"


def emit_schema() -> Path:
    """Write the fresh JSON Schema and return its path."""
    schema = SchemaDigest.model_json_schema()
    _SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _SCHEMA_PATH.open("w", encoding="utf-8") as fh:
        json.dump(schema, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return _SCHEMA_PATH


if __name__ == "__main__":
    out = emit_schema()
    # Module is invoked as ``python -m`` — print is the right channel here.
    print(f"Wrote {out}")
