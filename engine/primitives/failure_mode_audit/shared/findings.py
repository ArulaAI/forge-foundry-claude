"""Finding dataclass + Severity literal — the core data model for C10 audits.

Foundry-native module (no prowess vendor source). Used by all stack/output/llm
modules in C10. Kept separate from auditor.py so it can be imported without
pulling the auditor dispatch logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]
SEVERITY_ORDER: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class Finding:
    """A single failure-mode audit finding.

    Attributes:
        mode: Failure-mode ID (e.g., "fm1", "fm5").
        severity: One of low/medium/high/critical.
        file: Path to the file where the finding was detected (absolute or repo-relative).
        line: 1-indexed line number.
        message: Human-readable finding description.
        snippet: The source line text (or short surrounding context).
        rule_id: Identifier of the rule that fired (e.g., "fm5.hardcoded-secret.aws").
        cwe_id: CWE ID for FM5 findings (e.g., "CWE-798"); None for FM1-FM4.
    """

    mode: str
    severity: Severity
    file: str
    line: int
    message: str
    snippet: str
    rule_id: str
    cwe_id: str | None = None
