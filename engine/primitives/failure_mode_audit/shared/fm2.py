"""FM2 (context-window blindness) detector for Python.

Foundry-native (prowess U1 had FM2 in the rules YAML but the auditor's
audit_path() function never invoked it — it was stubbed). Walks AST per
file; for each `from X import ...` statement, resolves the target file
path within the project tree and flags refs where target file > N lines
or > M directory hops away.

Heuristic: high false-positive rate in monorepos is expected; severity is
intentionally `low` so it doesn't gate but still surfaces in reports.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import walk_source_files
from primitives.failure_mode_audit.shared.findings import Finding

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.failure_mode_audit.shared.config import AuditConfig


def _resolve_module_to_file(module: str, project_root: Path) -> Path | None:
    """Try to resolve a dotted module path to a file under project_root."""
    parts = module.split(".")
    candidate = project_root.joinpath(*parts).with_suffix(".py")
    if candidate.is_file():
        return candidate
    init_candidate = project_root.joinpath(*parts) / "__init__.py"
    if init_candidate.is_file():
        return init_candidate
    return None


def _line_count(file: Path) -> int:
    try:
        return file.read_text(encoding="utf-8").count("\n") + 1
    except (UnicodeDecodeError, PermissionError):
        return 0


def _dir_hops(caller: Path, target: Path) -> int:
    """Number of directory hops between caller and target file."""
    try:
        caller_dir = caller.parent.resolve()
        target_dir = target.parent.resolve()
        # Find common ancestor
        caller_parts = caller_dir.parts
        target_parts = target_dir.parts
        common_len = 0
        for a, b in zip(caller_parts, target_parts, strict=False):
            if a == b:
                common_len += 1
            else:
                break
        caller_depth = len(caller_parts) - common_len
        target_depth = len(target_parts) - common_len
        return caller_depth + target_depth
    except (ValueError, OSError):
        return 0


def audit_python_fm2(
    path: Path,
    *,
    project_root: Path,
    config: AuditConfig,
) -> list[Finding]:
    """Audit *path* for FM2 cross-file context blindness.

    Args:
        path: Single .py file or directory of .py files.
        project_root: Project root for resolving from-imports to file paths.
        config: AuditConfig — uses fm2_context_window thresholds; respects enabled flag.
    """
    if not config.fm2_context_window.enabled:
        return []

    threshold_lines = config.fm2_context_window.threshold_target_lines
    threshold_hops = config.fm2_context_window.threshold_dir_hops

    findings: list[Finding] = []
    py_files = list(walk_source_files(path, {".py"}, include_vendored=config.include_vendored))

    for caller in py_files:
        try:
            source = caller.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(caller))
        except (UnicodeDecodeError, PermissionError, SyntaxError):
            continue

        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            target = _resolve_module_to_file(node.module, project_root)
            if target is None or target == caller:
                continue
            target_lines = _line_count(target)
            hops = _dir_hops(caller, target)
            if target_lines > threshold_lines or hops > threshold_hops:
                line_text = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
                findings.append(
                    Finding(
                        mode="fm2",
                        severity="low",
                        file=str(caller),
                        line=node.lineno,
                        message=(
                            f"Cross-file ref to `{node.module}` "
                            f"(target {target_lines} lines, {hops} dir hops) — "
                            "AI may not have full context of the target"
                        ),
                        snippet=line_text.strip(),
                        rule_id="fm2.python.large-or-far-target",
                    )
                )
    return findings
