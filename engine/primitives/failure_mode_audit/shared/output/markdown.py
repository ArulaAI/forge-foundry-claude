"""Markdown renderer for failure-mode audit findings."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.failure_mode_audit.shared.findings import Finding


def render_markdown(findings: list[Finding], *, audit_path: str) -> str:
    """Render findings as a markdown report grouped by mode."""
    if not findings:
        return f"# Failure-mode audit: {audit_path}\n\nNo findings.\n"

    lines: list[str] = [f"# Failure-mode audit: {audit_path}", ""]
    by_mode: dict[str, list[Finding]] = {}
    for f in findings:
        by_mode.setdefault(f.mode, []).append(f)

    for mode in sorted(by_mode.keys()):
        mode_findings = by_mode[mode]
        lines.append(f"## {mode.upper()} ({len(mode_findings)} findings)")
        lines.append("")
        for f in mode_findings:
            lines.append(f"- **{f.severity}** `{f.file}:{f.line}` — {f.message}")
            lines.append("  ```")
            lines.append(f"  {f.snippet}")
            lines.append("  ```")
        lines.append("")

    return "\n".join(lines)
