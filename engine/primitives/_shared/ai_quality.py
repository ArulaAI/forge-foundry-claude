"""Shared AI-quality aggregation core (sub-call rule §3.4).

Extracted from the seven near-duplicate ``shared/ai_quality_aggregator.py``
modules (QUAL refactor, #722). The C10 (``failure_mode_audit``) + C11
(``production_failure_audit``) invocation, the :class:`AIQualityReport` and
:class:`DedupEntry` dataclasses, and the summary-string formatting are identical
across every consumer and live here once. The only genuinely per-primitive piece
— the dedup discipline against that primitive's canonical findings (different
finding type, ``(file, line)`` key extraction, FM-mode filter, and note/log text)
— stays in each primitive's own ``shared/ai_quality_aggregator.py`` as a small
``_dedup_against_*`` callable and a thin public ``aggregate_ai_quality`` wrapper.

This module imports only the failure-mode auditors' *public* surface
(``primitives.failure_mode_audit`` / ``primitives.production_failure_audit``),
never another primitive's ``shared/`` internals, so it is import-safe under the
QUAL-2 (#163) cross-primitive guard. Primitives with an extra report field
(``architecture_review.fm10_findings``, ``data_flow_map.fm8_findings``) subclass
:class:`AIQualityReport` and add the property locally.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from primitives.failure_mode_audit import Finding
from primitives.failure_mode_audit import audit_path as c10_audit
from primitives.production_failure_audit import (
    audit_production_path as c11_audit,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)


@dataclass
class DedupEntry:
    """A C10/C11 finding that was deduplicated against a canonical finding.

    ``note`` records why the finding was moved (the canonical finding it
    overlaps). The wording is primitive-specific and set by the caller.
    """

    finding: Finding
    note: str


@dataclass
class AIQualityReport:
    """Bundled findings from the C10 and C11 sub-calls, with dedup applied.

    ``deduplicated_findings`` holds C10/C11 findings that overlapped a canonical
    finding at the same ``file:line`` — they are MOVED here (not double-counted).
    Primitives that do not run dedup simply leave it empty (the default).
    """

    c10_findings: list[Finding]
    c11_findings: list[Finding]
    summary: str
    deduplicated_findings: list[DedupEntry] = field(default_factory=list)

    @cached_property
    def all_findings(self) -> list[Finding]:
        """Combined non-deduplicated findings from both auditors.

        Memoized via :func:`functools.cached_property` — the list is built once
        on first access and reused on subsequent calls (e.g. ``has_critical``
        reuses the result without rebuilding the concatenation).
        """
        return self.c10_findings + self.c11_findings

    @property
    def has_critical(self) -> bool:
        """True if any remaining (non-deduped) finding has severity == 'critical'."""
        return any(f.severity == "critical" for f in self.all_findings)


def run_c10_c11(
    code_path: Path,
) -> tuple[list[Finding], list[Finding]]:
    """Invoke C10 (FM1-FM5) and C11 (FM6-FM10) deterministically on *code_path*.

    Returns ``(c10_findings, c11_findings)``.
    """
    c10_findings = c10_audit(code_path)
    c11_findings = c11_audit(code_path)
    return c10_findings, c11_findings


def format_summary(
    c10_findings: list[Finding],
    c11_findings: list[Finding],
    *,
    n_dedup: int = 0,
    extra_clauses: Sequence[str] = (),
) -> str:
    """Build the AI-quality summary string shared by every aggregator.

    Reproduces the historical per-primitive summaries byte-for-byte:

    - the empty message fires only when there are no findings *and* nothing was
      deduplicated (``n_dedup`` defaults to 0 for the no-dedup primitives);
    - otherwise the base ``"N AI quality finding(s): … Critical: K."`` line is
      followed by any *extra_clauses* (e.g. the dedup count, or an FM10/FM8
      folding note), each separated by a single space.
    """
    n_c10 = len(c10_findings)
    n_c11 = len(c11_findings)
    total = n_c10 + n_c11

    if total == 0 and n_dedup == 0:
        return "No AI quality findings from C10 (FM1-FM5) or C11 (FM6-FM10)."

    critical_count = sum(
        1 for f in itertools.chain(c10_findings, c11_findings) if f.severity == "critical"
    )
    summary = (
        f"{total} AI quality finding(s): "
        f"{n_c10} from C10 (FM1-FM5), {n_c11} from C11 (FM6-FM10). "
        f"Critical: {critical_count}."
    )
    for clause in extra_clauses:
        summary += " " + clause
    return summary


def dedup_against_canonical(
    c10_findings: list[Finding],
    c11_findings: list[Finding],
    canonical_keys: set[tuple[str, int]],
    *,
    c10_modes: frozenset[str] | None,
    c11_modes: frozenset[str] | None,
    note: Callable[[str, Finding], str],
    log_line: Callable[[str, Finding], str],
) -> tuple[list[Finding], list[Finding], list[DedupEntry]]:
    """Move C10/C11 findings that overlap a canonical finding's ``(file, line)``.

    This is the shared two-loop dedup discipline lifted from the per-primitive
    ``shared/ai_quality_aggregator.py`` modules (#815). The only genuinely
    per-primitive variation is captured by the parameters:

    - *canonical_keys* — the ``{(file, line), (basename, line)}`` set the caller
      builds from its own canonical findings (the key extraction differs: some
      primitives read ``issue.file``/``issue.line`` directly, others walk nested
      ``trace.code_elements``).
    - *c10_modes* / *c11_modes* — the FM-mode allow-list for each auditor.
      ``None`` means "no mode filter" (every finding is eligible, the historical
      code_review / spec_validate behaviour); an empty ``frozenset()`` means "no
      finding is eligible" (api_contract_review's C10, which deduped nothing).
    - *note* — ``(tag, finding) -> str`` producing the :class:`DedupEntry` note,
      where ``tag`` is ``"C10"`` or ``"C11"``.
    - *log_line* — ``(tag, finding) -> str`` producing the stderr diagnostic.

    A finding is moved (recorded in ``dedup_entries``, dropped from the
    remaining list) only when its mode is eligible *and* its ``(file, line)`` or
    ``(basename, line)`` is present in *canonical_keys*. Returns
    ``(remaining_c10, remaining_c11, dedup_entries)``.
    """
    remaining_c10: list[Finding] = []
    remaining_c11: list[Finding] = []
    dedup_entries: list[DedupEntry] = []

    for findings, modes, tag, remaining in (
        (c10_findings, c10_modes, "C10", remaining_c10),
        (c11_findings, c11_modes, "C11", remaining_c11),
    ):
        for finding in findings:
            mode_eligible = modes is None or finding.mode.lower() in modes
            overlaps = (finding.file, finding.line) in canonical_keys or (
                Path(finding.file).name,
                finding.line,
            ) in canonical_keys
            if mode_eligible and overlaps:
                dedup_entries.append(DedupEntry(finding=finding, note=note(tag, finding)))
                logger.warning("%s", log_line(tag, finding))
            else:
                remaining.append(finding)

    return remaining_c10, remaining_c11, dedup_entries
