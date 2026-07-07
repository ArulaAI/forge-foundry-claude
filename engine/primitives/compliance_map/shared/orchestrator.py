"""S70 /compliance-map — Orchestrator helpers.

The harness-driven ``compliance_map(...)`` orchestrator was removed with the
harness layer (the CLI now runs the skill-first extract→judge→continue
protocol). Only the deterministic helpers that the CLI still imports remain.
"""

from __future__ import annotations


def _extract_title(spec_text: str) -> str:
    """Extract the first H1 heading from spec text, or a truncated fallback."""
    for line in spec_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    # Fallback: first non-empty line, truncated
    for line in spec_text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:80]
    return "Untitled Spec"
