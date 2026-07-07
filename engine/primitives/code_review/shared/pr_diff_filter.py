"""C4 /code-review — PR diff filter.

Re-exports the shared implementation from compliance_map.
When --git-diff REF...REF is given, narrows the file list to only
files changed in the diff range.
"""

from __future__ import annotations

# Re-export the shared implementation — do NOT duplicate logic.
from primitives.compliance_map import (
    GitDiffError,
    get_changed_files,
)

__all__ = ["GitDiffError", "get_changed_files"]
