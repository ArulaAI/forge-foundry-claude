"""FM10 architectural-decay detector.

Reads .foundry/architecture.yaml (layers + module_to_layer mapping).
Flags imports that violate layer direction (lower-level importing
higher-level). Also detects simple A<->B cyclic imports.

Returns [] when architecture.yaml is absent (no false-positives on
projects without a declared architecture).

Only runs when config.production.fm10_arch_decay.enabled is True.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any

import yaml

from primitives._shared.scan_exclusions import walk_source_files
from primitives.production_failure_audit.shared.findings import Finding

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.failure_mode_audit import AuditConfig


def _load_architecture_yaml(project_root: Path, yaml_path: str) -> dict[str, Any] | None:
    arch_path = project_root / yaml_path
    if not arch_path.is_file():
        return None
    try:
        data = yaml.safe_load(arch_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _module_layer(module: str, module_to_layer: dict[str, str]) -> str | None:
    """Find longest-prefix match in module_to_layer."""
    candidates = [m for m in module_to_layer if module == m or module.startswith(m + ".")]
    if not candidates:
        return None
    longest = max(candidates, key=len)
    return module_to_layer[longest]


def _extract_imports(file: Path) -> list[tuple[int, str]]:
    """Return list of (line_number, module_name).

    For ``from X import Y`` we emit both ``X`` (for layer-boundary checks)
    and ``X.Y`` (so that cycle detection can match fully-qualified sub-modules
    when the callee uses ``from package import submodule`` syntax).
    """
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, PermissionError):
        return []
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
            # Also emit fully-qualified names for each imported name so that
            # cycle detection works when callers write `from pkg import submod`.
            for alias in node.names:
                imports.append((node.lineno, f"{node.module}.{alias.name}"))
    return imports


def _build_file_module_map(py_files: list[Path], project_root: Path) -> dict[Path, str]:
    """Map each .py file to its dotted module name relative to project_root."""
    file_to_module: dict[Path, str] = {}
    for f in py_files:
        try:
            rel = f.relative_to(project_root)
        except ValueError:
            continue
        parts = rel.with_suffix("").parts
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            file_to_module[f] = ".".join(parts)
    return file_to_module


def _detect_layer_violations(
    file_to_module: dict[Path, str],
    module_to_layer: dict[str, str],
    layer_index: dict[str, int],
) -> list[Finding]:
    """Flag imports that cross from a lower layer up into a higher layer."""
    findings: list[Finding] = []
    for f, mod in file_to_module.items():
        caller_layer = _module_layer(mod, module_to_layer)
        if caller_layer is None or caller_layer not in layer_index:
            continue
        caller_idx = layer_index[caller_layer]
        for line_num, imp in _extract_imports(f):
            target_layer = _module_layer(imp, module_to_layer)
            if target_layer is None or target_layer not in layer_index:
                continue
            if layer_index[target_layer] > caller_idx:
                findings.append(
                    Finding(
                        mode="fm10",
                        severity="high",
                        file=str(f),
                        line=line_num,
                        message=(
                            f"Layer violation: `{caller_layer}` module imports"
                            f" `{target_layer}` (target imports must stay at"
                            " or below current layer)"
                        ),
                        snippet=f"import {imp}",
                        rule_id="fm10.layer-violation",
                        cwe_id="CWE-1061",
                    )
                )
    return findings


def _detect_cycles(
    module_imports: dict[str, set[str]],
    file_to_module: dict[Path, str],
) -> list[Finding]:
    """Flag A↔B cyclic imports (module A imports B and B imports A)."""
    findings: list[Finding] = []
    seen_cycles: set[frozenset[str]] = set()
    for mod_a, imports_a in module_imports.items():
        for mod_b in imports_a:
            if mod_a == mod_b:
                continue  # skip self-import (e.g., __init__.py re-exports)
            if mod_b not in module_imports or mod_a not in module_imports[mod_b]:
                continue
            pair = frozenset({mod_a, mod_b})
            if pair in seen_cycles:
                continue
            seen_cycles.add(pair)
            for f, m in file_to_module.items():
                if m == mod_a:
                    findings.append(
                        Finding(
                            mode="fm10",
                            severity="medium",
                            file=str(f),
                            line=1,
                            message=f"Cyclic import: {mod_a} ↔ {mod_b}",
                            snippet=f"import {mod_b}",
                            rule_id="fm10.cycle",
                            cwe_id="CWE-1061",
                        )
                    )
                    break
    return findings


def audit_fm10(path: Path, *, project_root: Path, config: AuditConfig) -> list[Finding]:
    """Audit path for FM10. Returns [] if config disabled OR no architecture.yaml present."""
    if not config.production.fm10_arch_decay.enabled:
        return []
    arch_yaml_path = config.production.fm10_arch_decay.architecture_yaml
    arch = _load_architecture_yaml(project_root, arch_yaml_path)
    if arch is None:
        return []
    layers: list[str] = arch.get("layers", [])
    module_to_layer: dict[str, str] = arch.get("module_to_layer", {})
    if not layers or not module_to_layer:
        return []

    layer_index = {name: i for i, name in enumerate(layers)}
    py_files = list(walk_source_files(path, {".py"}, include_vendored=config.include_vendored))
    file_to_module = _build_file_module_map(py_files, project_root)
    module_imports: dict[str, set[str]] = {
        mod: {imp for _, imp in _extract_imports(f)} for f, mod in file_to_module.items()
    }

    findings = _detect_layer_violations(file_to_module, module_to_layer, layer_index)
    findings.extend(_detect_cycles(module_imports, file_to_module))
    return findings
