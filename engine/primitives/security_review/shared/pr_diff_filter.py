"""S11 /security-review — PR diff filter.

Re-export from compliance_map.shared.pr_diff_filter per spec B2 instruction.
No duplication — the compliance_map module owns this implementation.
"""

from __future__ import annotations

# Re-export from S70 compliance_map — don't duplicate.
from primitives.compliance_map import (
    GitDiffError,
    get_changed_files,
)

__all__ = ["GitDiffError", "get_changed_files"]
