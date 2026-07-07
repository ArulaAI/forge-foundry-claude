"""Load + validate the committed engine artifact schemas (Layer 1).

The four engine artifacts (``facts``, ``digest``, ``system_map``, ``roadmap``)
are persisted as JSON so M3/M4/M5 pipelines and **external, cross-language
consumers** can lock against a stable JSON Schema without importing the Python
model (design spec §2 two-artifact discipline). ``load_schema`` exposes the
committed schema for those consumers.

In-process validation goes through the source-of-truth Pydantic model the schema
was generated from — the repo convention (``jsonschema`` is a dev-only dependency
and must not be imported by a shipped package module). ``validate_artifact`` is
therefore an equivalent, dependency-free check: it round-trips the decoded JSON
through the model and raises on mismatch.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from primitives._shared.modernization.models import (
    EngineFacts,
    ModernizationDigest,
    Roadmap,
    SystemMap,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

#: Logical artifact name → committed schema filename.
SCHEMA_FILES: Mapping[str, str] = {
    "facts": "facts.schema.json",
    "digest": "digest.schema.json",
    "system_map": "system_map.schema.json",
    "roadmap": "roadmap.schema.json",
}

#: Logical artifact name → the Pydantic model that owns its schema.
_ARTIFACT_MODELS: Mapping[str, type[BaseModel]] = {
    "facts": EngineFacts,
    "digest": ModernizationDigest,
    "system_map": SystemMap,
    "roadmap": Roadmap,
}


def _model_for(name: str) -> type[BaseModel]:
    try:
        return _ARTIFACT_MODELS[name]
    except KeyError:
        known = ", ".join(sorted(_ARTIFACT_MODELS))
        raise KeyError(f"unknown engine artifact {name!r} (known: {known})") from None


@cache
def load_schema(name: str) -> dict[str, object]:
    """Return the committed JSON Schema for an engine artifact (cached).

    Args:
        name: one of :data:`SCHEMA_FILES` (``facts``/``digest``/``system_map``/``roadmap``).

    Raises:
        KeyError: *name* is not a known engine artifact.
    """
    filename = SCHEMA_FILES.get(name)
    if filename is None:
        known = ", ".join(sorted(SCHEMA_FILES))
        raise KeyError(f"unknown engine artifact {name!r} (known: {known})") from None
    loaded: dict[str, object] = json.loads((_SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    return loaded


def validate_artifact(name: str, data: object) -> BaseModel:
    """Validate a decoded JSON *data* artifact against its schema's model.

    Equivalent to JSON-Schema validation (the committed schema is generated from
    this exact model) but without the dev-only ``jsonschema`` runtime dependency.

    Args:
        name: the artifact name (see :data:`SCHEMA_FILES`).
        data: the decoded JSON (e.g. ``json.loads(path.read_text())``).

    Returns:
        The validated model instance.

    Raises:
        KeyError: *name* is not a known engine artifact.
        pydantic.ValidationError: *data* does not conform to the schema.
    """
    return _model_for(name).model_validate(data)
