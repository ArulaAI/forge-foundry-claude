"""S60 /data-flow-map — Stage B: Data flow tracing.

Python full (intra-module via AST):
  - Assignments: y = source_variable
  - Function calls: f(source_variable)
  - Attribute access: obj.source_field

TS/Java: module-level only (not yet implemented).
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import is_excluded as _is_excluded
from primitives.data_flow_map.shared.models import FlowEdge, PIISource

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Python AST flow tracer
# ---------------------------------------------------------------------------


class _FlowVisitor(ast.NodeVisitor):
    """AST visitor that tracks flow of named PII sources to downstream usages."""

    def __init__(self, pii_names: set[str]) -> None:
        self._pii_names = pii_names
        self._flows: list[FlowEdge] = []
        # Map of variable name → source name (so we can track aliases)
        self._alias: dict[str, str] = {}
        # Seed the alias map with known PII names
        for name in pii_names:
            self._alias[name] = name

    @property
    def flows(self) -> list[FlowEdge]:
        return self._flows

    def _source_name(self, name: str) -> str | None:
        """Return the canonical PII source name if *name* is tracked, else None."""
        return self._alias.get(name)

    def _add_edge(self, from_: str, to_: str, transformation: str | None = None) -> None:
        edge = FlowEdge(from_node=from_, to_node=to_, transformation=transformation)
        self._flows.append(edge)

    # ------------------------------------------------------------------
    # Assignments: y = source  or  y = transform(source)
    # ------------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        """y = <expr> — if rhs references a PII source, record flow."""
        referenced = self._pii_names_in_expr(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name) and referenced:
                for src in referenced:
                    self._add_edge(
                        from_=src,
                        to_=target.id,
                        transformation=ast.unparse(node.value) if hasattr(ast, "unparse") else None,
                    )
                    # Alias the new variable as a proxy for the source
                    self._alias[target.id] = src
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """x: T = <expr>"""
        if node.value is not None:
            referenced = self._pii_names_in_expr(node.value)
            if isinstance(node.target, ast.Name) and referenced:
                for src in referenced:
                    self._add_edge(
                        from_=src,
                        to_=node.target.id,
                        transformation=None,
                    )
                    self._alias[node.target.id] = src
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Function calls: f(source) or obj.method(source)
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        """f(source) — if any arg is a tracked PII name, record flow."""
        # Determine call target name
        if isinstance(node.func, ast.Name):
            callee = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee = ast.unparse(node.func) if hasattr(ast, "unparse") else node.func.attr
        else:
            callee = "<call>"

        for arg in node.args:
            if isinstance(arg, ast.Name):
                src = self._source_name(arg.id)
                if src is not None:
                    self._add_edge(
                        from_=src,
                        to_=callee,
                        transformation="function_arg",
                    )

        # f-string and format call arguments
        for kw in node.keywords:
            if isinstance(kw.value, ast.Name):
                src = self._source_name(kw.value.id)
                if src is not None:
                    self._add_edge(
                        from_=src,
                        to_=callee,
                        transformation="keyword_arg",
                    )

        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Attribute access: obj.pii_field
    # ------------------------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """obj.field — if field name is a PII source, record flow."""
        if node.attr in self._pii_names:
            obj_name = (
                ast.unparse(node.value)
                if hasattr(ast, "unparse")
                else getattr(node.value, "id", "<obj>")
            )
            self._add_edge(
                from_=node.attr,
                to_=f"{obj_name}.{node.attr}",
                transformation="attribute_access",
            )
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # JoinedStr (f-strings): f"... {email} ..."
    # ------------------------------------------------------------------

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        """f-string: check if any interpolated value references a PII source."""
        for value in node.values:
            if isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
                src = self._source_name(value.value.id)
                if src is not None:
                    self._add_edge(
                        from_=src,
                        to_="<f-string>",
                        transformation="string_interpolation",
                    )
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Helper: collect all PII source names referenced in an expression
    # ------------------------------------------------------------------

    def _pii_names_in_expr(self, expr: ast.expr) -> list[str]:
        """Return list of PII source names referenced in *expr*."""
        refs: list[str] = []
        for node in ast.walk(expr):
            if isinstance(node, ast.Name):
                src = self._source_name(node.id)
                if src is not None and src not in refs:
                    refs.append(src)
        return refs


def _trace_python_file(path: Path, pii_names: set[str]) -> list[FlowEdge]:
    """Run flow tracing on a single Python file."""
    try:
        source_code = path.read_text(encoding="utf-8")
        tree = ast.parse(source_code)
    except (SyntaxError, UnicodeDecodeError):
        return []
    visitor = _FlowVisitor(pii_names)
    visitor.visit(tree)
    return visitor.flows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def trace_flows(
    code_path: Path,
    sources: list[PIISource],
    language: str = "auto",
    *,
    include_vendored: bool = False,
) -> list[FlowEdge]:
    """Build the flow graph connecting PII sources to downstream usages.

    For Python, uses full AST tracing. For TS/Java, flow tracing is not yet
    implemented (module-level only; empty list returned for those languages).

    Args:
        code_path: Directory or single file to trace.
        sources: Detected PIISource objects from Stage A.
        language: 'python', 'java', 'ts', or 'auto'.

    Returns:
        List of FlowEdge objects (no deduplication — same edge can appear from
        multiple call sites; callers may deduplicate if needed).
    """
    if not sources:
        return []

    pii_names = {s.name for s in sources}
    flows: list[FlowEdge] = []

    files: list[Path]
    files = (
        [code_path]
        if code_path.is_file()
        else [
            p
            for p in code_path.rglob("*")
            if not p.is_symlink()
            and not _is_excluded(p, code_path, include_vendored=include_vendored)
        ]
    )

    for file_path in sorted(files):
        if not file_path.is_file():
            continue
        suffix = file_path.suffix.lower()

        if language == "auto":
            if suffix != ".py":
                continue
            lang = "python"
        elif language == "python" and suffix == ".py":
            lang = "python"
        elif language in {"ts", "java"}:
            # TS/Java flow tracing: not implemented for v0.12.0
            continue
        else:
            continue

        if lang == "python":
            flows.extend(_trace_python_file(file_path, pii_names))

    return flows
