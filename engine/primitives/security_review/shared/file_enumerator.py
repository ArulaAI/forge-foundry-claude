"""S11 /security-review — File enumerator.

Walk code_path respecting --file-pattern glob, language auto-detect, and
always-excluded noise directories. Same pattern as code_review (C4).
"""

from __future__ import annotations

# Re-use C4's file_enumerator directly — no duplication.
from primitives.code_review import (
    ALWAYS_EXCLUDED_DIRS,
    LANGUAGE_EXTENSIONS,
    enumerate_files,
)

__all__ = ["ALWAYS_EXCLUDED_DIRS", "LANGUAGE_EXTENSIONS", "enumerate_files"]
