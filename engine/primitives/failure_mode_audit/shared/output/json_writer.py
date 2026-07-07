"""JSON output renderer (schema-versioned wrapper)."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.failure_mode_audit.shared.findings import Finding

JSON_SCHEMA_VERSION = "1.0"


def render_json(findings: list[Finding], *, report_type: str | None = None) -> str:
    """Render findings as a schema-versioned JSON document.

    Top-level shape:
        {"schema_version": "1.0", "report_type": "<type>"?, "findings": [Finding, ...]}

    ``report_type`` is a stable, content-independent discriminator (e.g.
    ``c10_failure_mode_audit`` / ``c11_production_failure_audit``). It lets a
    downstream consumer (C6 confidence-gate) classify the report even when
    ``findings`` is empty — an empty C10 and an empty C11 report are otherwise
    byte-identical since both share this writer (#1195 4c). Omitted from the
    output when None (back-compat for callers that don't set it).

    Future v0.3.0+ may bump schema_version when adding fields.
    """
    data: dict[str, object] = {"schema_version": JSON_SCHEMA_VERSION}
    if report_type is not None:
        data["report_type"] = report_type
    data["findings"] = [asdict(f) for f in findings]
    return json.dumps(data, indent=2, ensure_ascii=False)
