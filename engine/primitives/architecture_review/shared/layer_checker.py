"""C8 /architecture-review — Stage B: layer violation checker.

Reads .foundry/architecture.yaml and compares dep graph edges to the declared
layer order. Flags edges that violate the declared direction.

Architecture YAML format (for C8's purposes):
  layers: ["web", "service", "data"]   # ordered: index 0 is top, higher index is lower
  module_to_layer:
    web: web
    service: service
    data: data

Layer rule: a module in layer[i] may import from layer[j] only if j >= i (lower or same).
Importing from a higher layer (lower index) is a violation.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import yaml

from primitives.architecture_review.shared.models import DepGraph, LayerViolation

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.architecture_review.shared.runtime_context import RuntimeContext


def _load_arch_yaml(yaml_path: Path) -> dict[str, Any] | None:
    """Load and parse .foundry/architecture.yaml. Returns None if missing or invalid."""
    if not yaml_path.is_file():
        return None
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(
            f"[architecture-review] WARNING: could not parse {yaml_path}: {exc}",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def _module_layer(module_name: str, module_to_layer: dict[str, str]) -> str | None:
    """Find the layer for a module name using longest-prefix matching."""
    candidates = [m for m in module_to_layer if module_name == m or module_name.startswith(m + ".")]
    if not candidates:
        return None
    longest = max(candidates, key=len)
    return module_to_layer[longest]


def detect_layer_violations(
    dep_graph: DepGraph,
    arch_yaml_path: Path | None = None,
    code_path: Path | None = None,
    runtime_ctx: RuntimeContext | None = None,
) -> list[LayerViolation]:
    """Detect layer violations in the dependency graph.

    Args:
        dep_graph: The static dependency graph (Stage A output).
        arch_yaml_path: Explicit path to architecture.yaml. If None, looks in
            code_path/.foundry/architecture.yaml.
        code_path: Code directory (used to auto-find architecture.yaml).
        runtime_ctx: Optional cross-primitive context loaded from
            ``.foundry/context/`` (see :mod:`runtime_context`). When the
            context carries S10 trust-boundary components, each
            LayerViolation whose endpoints map to two distinct
            components in that set is flagged with
            ``trust_boundary_violation=True``. When *runtime_ctx* is
            None or empty, behavior is identical to v0.11.0.

    Returns:
        List of LayerViolation objects (may be empty).
    """
    # Resolve arch yaml path
    resolved_yaml: Path | None = arch_yaml_path
    if resolved_yaml is None and code_path is not None:
        candidate = code_path / ".foundry" / "architecture.yaml"
        if candidate.is_file():
            resolved_yaml = candidate

    if resolved_yaml is None:
        return []

    arch = _load_arch_yaml(resolved_yaml)
    if arch is None:
        return []

    layers: list[str] = arch.get("layers", [])
    module_to_layer: dict[str, str] = arch.get("module_to_layer", {})

    if not layers or not module_to_layer:
        return []

    layer_index = {name: i for i, name in enumerate(layers)}
    layer_direction_str = " -> ".join(layers)

    violations: list[LayerViolation] = []
    for from_mod, to_mod in dep_graph.edges:
        from_layer = _module_layer(from_mod, module_to_layer)
        to_layer = _module_layer(to_mod, module_to_layer)

        if from_layer is None or to_layer is None:
            continue
        if from_layer not in layer_index or to_layer not in layer_index:
            continue

        from_idx = layer_index[from_layer]
        to_idx = layer_index[to_layer]

        # Violation: a module imports from a layer with LOWER index (higher-level layer)
        # e.g., data (index 2) imports web (index 0) → illegal
        if to_idx < from_idx:
            # Cross-primitive: ask S10 whether this edge straddles a
            # declared trust boundary. The module_to_layer mapping
            # doubles as a module-to-component lookup when the operator
            # aligns layer/component names across .foundry/architecture.yaml
            # and S10's threats[].component values.
            trust_violation = False
            if runtime_ctx is not None:
                trust_violation = runtime_ctx.is_trust_boundary_crossing(from_layer, to_layer)
            violations.append(
                LayerViolation(
                    from_module=from_mod,
                    to_module=to_mod,
                    expected_direction=layer_direction_str,
                    actual_direction=(
                        f"{from_layer} ({from_idx}) -> {to_layer} ({to_idx}) [reversed]"
                    ),
                    severity="high",
                    source="c8_static",
                    trust_boundary_violation=trust_violation,
                )
            )

    return violations
