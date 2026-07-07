"""C5 /spec-validate — Stage A: acceptance consumer.

Pure re-export of ``primitives._shared.acceptance``.
C5 reuses the shared parser verbatim — no duplication.

Public API:
    parse_acceptance(spec_markdown: str) -> AcceptanceDoc
    AcceptanceCriterion
    AcceptanceDoc
"""

from __future__ import annotations

# Re-export everything needed by C5 from the shared acceptance module.
# Do NOT duplicate the parsing logic here.
from primitives._shared.acceptance import (
    AcceptanceCriterion,
    AcceptanceDoc,
    parse_acceptance,
)

__all__ = [
    "AcceptanceCriterion",
    "AcceptanceDoc",
    "parse_acceptance",
]
