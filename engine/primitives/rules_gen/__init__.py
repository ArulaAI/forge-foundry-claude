"""R1 /rules-gen — public API.

Scaffolded by R1-289 (#289). Catalog loader + Pydantic models land with
R1-290 (#290); the dual-harness rule renderer lands with R1-291 (#291).

See ``primitives/rules_gen/SPEC.md`` for the primitive contract.
"""

from __future__ import annotations

from primitives.rules_gen.shared.catalog_loader import (
    Catalog,
    CatalogNotFoundError,
    CatalogParseError,
    RuleSection,
    load_catalog,
    load_catalogs,
)
from primitives.rules_gen.shared.generator import (
    GenerateResult,
    generate_rules,
)
from primitives.rules_gen.shared.rule_renderer import (
    render_claude_rule,
    render_copilot_instruction,
)

__all__ = [
    "Catalog",
    "CatalogNotFoundError",
    "CatalogParseError",
    "GenerateResult",
    "RuleSection",
    "generate_rules",
    "load_catalog",
    "load_catalogs",
    "render_claude_rule",
    "render_copilot_instruction",
]
