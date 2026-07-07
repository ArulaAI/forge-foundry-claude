"""S60 /data-flow-map — Classification catalog loader.

Loads the starter YAML (ships with the package) plus an optional custom YAML.
Custom entries win on pattern conflict (deduplication by pattern string).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from primitives.data_flow_map.shared.models import (  # noqa: TC001 — Pydantic fields in ClassificationEntry
    Category,
    DataClassification,
    Severity,
)

_STARTER_PATH = Path(__file__).parent / "classification_starter.yaml"


class ClassificationEntry(BaseModel):
    """One entry from the classification catalog."""

    model_config = ConfigDict(extra="forbid")

    pattern: str
    classification: DataClassification
    category: Category
    severity: Severity
    note: str = ""
    retention_days: int | None = None


def _load_yaml_entries(path: Path) -> list[ClassificationEntry]:
    """Parse a YAML file and return a list of ClassificationEntry objects."""
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "fields" not in raw:
        raise ValueError(f"Classification YAML must have a 'fields' key: {path}")
    entries = []
    for item in raw["fields"]:
        entries.append(ClassificationEntry(**item))
    return entries


def load_classification(
    custom_path: Path | None = None,
) -> list[ClassificationEntry]:
    """Load starter + optional custom classification catalog.

    Entries are deduplicated by ``pattern``. If the same pattern appears in
    both starter and custom, the **custom** entry wins.

    Args:
        custom_path: Optional path to a custom YAML file (merged with starter).

    Returns:
        Merged, deduplicated list of ClassificationEntry objects.
    """
    starter_entries = _load_yaml_entries(_STARTER_PATH)

    # Index by pattern — starter first, custom overrides
    by_pattern: dict[str, ClassificationEntry] = {e.pattern: e for e in starter_entries}

    if custom_path is not None:
        custom_entries = _load_yaml_entries(custom_path)
        for entry in custom_entries:
            by_pattern[entry.pattern] = entry  # custom wins

    return list(by_pattern.values())
