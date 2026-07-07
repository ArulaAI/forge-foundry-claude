"""Discovery engine — Phase D deterministic validate-as-script auditor.

The polyglot Phase D self-audit. It verifies a rendered ``PLAYBOOK.md``
(Phase C output, see :mod:`primitives._shared.discovery.playbook`) against the
Phase B :class:`~primitives._shared.discovery.models.AnalysisDigest` and the
evidence ledger — in **pure Python**, with no second model re-read pass. The
model only ever fixes the items :func:`audit` flags (token-offload principle:
the deterministic engine does the verification; the model does the repair).

Five check families run, each contributing to the ``total_checks`` /
``passed_checks`` tally; every failure within a family becomes one
:class:`~primitives._shared.discovery.models.AuditFinding`:

1. **citation_missing** — every ``path:line`` reference in the playbook is
   validated against the real tree via the Layer-1
   :func:`primitives._shared.modernization.refcheck.check` engine. A playbook
   that cites no references contributes zero checks here (vacuously fine).
2. **entity_mismatch** — every candidate entity name harvested from the
   Markdown table rows and the mermaid node labels must appear in the
   entity-name universe (the union of the six abstract entity lists). A
   candidate that is not in the universe and is not a known structural token
   is a fabrication.
3. **mermaid_node_orphan** — the same orphan, surfaced under the mermaid lens:
   a mermaid node label not in the universe (and not a structural placeholder).
   This deliberately overlaps the entity pass for mermaid labels; the two
   families report the same orphan under their two lenses and count their
   checks independently.
4. **section_missing** — the headings ``## 1.`` … ``## {expected_sections}.``
   must all be present.
5. **synthesis_citation_unresolved** — when harness synthesis judgments are
   provided, every ``path:line`` entry in each section's ``citations`` list
   (and any inline ``path:line`` refs found in the section ``body``) is
   refchecked against ``code-root`` using the same Layer-1 refcheck engine as
   family 1. Unresolvable refs become findings in this family and are folded
   into the shared accuracy fraction alongside the deterministic citations.
   This family contributes zero checks when ``synthesis=None`` — the
   ``None`` path is byte-identical to the four-family behaviour. Introduced
   in #862.

``accuracy = passed_checks / total_checks`` (``1.0`` when ``total_checks == 0``
— nothing to audit is vacuously perfect). All finding lists are sorted
deterministically (by ``kind``, then ``message``) so the report is byte-stable
run-over-run.

Design spec §M5 Phase D
(``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``
§"Phase D — Self-Audit")
+ issue [#640](https://github.com/arula-ai/forge-foundry-primitives/issues/640).
Synthesis honesty guard: #862.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from primitives._shared.discovery.models import AuditFinding
from primitives._shared.modernization.refcheck import check as refcheck_check

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from primitives._shared.discovery.models import AnalysisDigest, EvidenceRow
    from primitives._shared.discovery.synthesis import SynthesisJudgments

# ---------------------------------------------------------------------------
# Structural-token vocabulary
# ---------------------------------------------------------------------------
#
# The deterministic Phase C renderer emits a fixed vocabulary of table
# headers and count-table category labels that are NOT entity names. A
# candidate matching one of these (case-insensitively for the category
# labels) is structural scaffolding, not a fabricated entity. Kept as a
# closed set so a future renderer column can be wired here without the
# auditor flagging the whole clean playbook.

#: Table header cells the renderer emits (first-cell position).
_HEADER_TOKENS: frozenset[str] = frozenset(
    {
        "Name",
        "Kind",
        "Route",
        "Roles",
        "LoC",
        "Source",
        "Requirement",
        "Acceptance Criteria",
        "Tests",
        "Finding",
        "Entity",
        "Entry Point",
        "Execution Unit",
        "Data Store",
        "Candidate",
        "Rule ID",
    }
)

#: §1 / §16 count-table data-row labels (``| Execution units | 5 |``) — these
#: are category labels, not entity names. Matched case-insensitively.
_COUNT_LABELS: frozenset[str] = frozenset(
    {
        "execution units",
        "entry points",
        "data stores",
        "ui elements",
        "batch units",
        "shared components",
    }
)

#: Em-dash / empty placeholders the renderer uses for "no value".
_EMPTY_TOKENS: frozenset[str] = frozenset({"", "—", "-"})

#: Markdown table separator row cell.
_SEPARATOR: str = "---"

#: Stable-id pattern (``R-001`` etc.) the renderer assigns; never an entity.
_ID_RE = re.compile(r"^[A-Z]+-\d+$")

#: Quoted mermaid node label (``n0["Widget"]`` → ``Widget``).
_MERMAID_LABEL_RE = re.compile(r'"([^"]*)"')

#: Opening / closing of a ```mermaid fenced block.
_MERMAID_OPEN: str = "```mermaid"
_FENCE: str = "```"


def _is_structural(candidate: str) -> bool:
    """True when ``candidate`` is renderer scaffolding, not a real entity name."""
    if candidate in _EMPTY_TOKENS or candidate == _SEPARATOR:
        return True
    if candidate in _HEADER_TOKENS:
        return True
    if candidate.lower() in _COUNT_LABELS:
        return True
    return bool(_ID_RE.match(candidate))


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


class AuditReport(BaseModel):
    """Phase D self-audit result.

    ``accuracy`` is the fraction of all deterministic checks that passed
    (``passed_checks / total_checks``, or ``1.0`` when there was nothing to
    check). ``findings`` is every flagged item, sorted deterministically by
    ``(kind, message)``. The report is appended to the playbook's
    §Validation Report section — Phase D never silently drops content.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    accuracy: float = Field(ge=0.0, le=1.0)
    findings: list[AuditFinding] = Field(default_factory=list)
    total_checks: int = Field(ge=0)
    passed_checks: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------


def _table_candidates(playbook_md: str) -> list[str]:
    """Return the first-cell value of every Markdown table data row.

    A table row is a line starting with ``|``. The first cell is stripped of
    surrounding whitespace and backticks. Separator rows (``| --- | --- |``)
    fall through as the ``---`` token, which :func:`_is_structural` filters.
    """
    candidates: list[str] = []
    for line in playbook_md.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = stripped.strip("|").split("|")
        if not cells:
            continue
        candidates.append(cells[0].strip().strip("`").strip())
    return candidates


def _mermaid_labels(playbook_md: str) -> list[str]:
    """Return the quoted node labels inside every ```mermaid fenced block.

    Only quoted ``"…"`` label text is harvested (the renderer always quotes
    node labels via ``render._quote_label``); edge labels and the
    ``flowchart``/``erDiagram`` keywords carry no quotes and are ignored.
    """
    labels: list[str] = []
    in_fence = False
    for line in playbook_md.splitlines():
        stripped = line.strip()
        if not in_fence and stripped == _MERMAID_OPEN:
            in_fence = True
            continue
        if in_fence and stripped == _FENCE:
            in_fence = False
            continue
        if in_fence:
            labels.extend(_MERMAID_LABEL_RE.findall(line))
    return labels


def _entity_universe(analysis: AnalysisDigest) -> set[str]:
    """Return every entity ``name`` across all six abstract entity lists."""
    universe: set[str] = set()
    lists: Iterable[Iterable[object]] = (
        analysis.execution_units,
        analysis.shared_components,
        analysis.entry_points,
        analysis.data_stores,
        analysis.ui_elements,
        analysis.batch_units,
    )
    for entities in lists:
        for entity in entities:
            universe.add(getattr(entity, "name"))  # noqa: B009 — structural access
    return universe


# ---------------------------------------------------------------------------
# Check families
# ---------------------------------------------------------------------------


def _citation_pass(playbook_md: str, repo_root: Path) -> tuple[int, int, list[AuditFinding]]:
    """Run the refcheck engine; one finding per broken ``path:line`` ref."""
    report = refcheck_check(playbook_md, repo_root)
    findings = [
        AuditFinding(
            kind="citation_missing",
            message=(
                f"broken/unresolved citation {broken.ref!r} ({broken.reason}) "
                f"at line {broken.source_line}"
            ),
            affected_path=broken.path,
            severity="high",
        )
        for broken in report.broken
    ]
    total = report.total_refs
    passed = total - len(report.broken)
    return total, passed, findings


def _entity_pass(
    playbook_md: str, universe: set[str], mermaid_labels: list[str]
) -> tuple[int, int, list[AuditFinding]]:
    """Flag table-row + mermaid-label candidates not in the entity universe.

    *mermaid_labels* is parsed once by the caller and shared with _mermaid_pass
    so the document is not mermaid-parsed twice per audit (#739).
    """
    candidates = [c for c in (*_table_candidates(playbook_md), *mermaid_labels)]
    checked = [c for c in candidates if not _is_structural(c)]
    findings = [
        AuditFinding(
            kind="entity_mismatch",
            message=f"name {candidate!r} not found in analysis",
            affected_path="",
            severity="high",
        )
        for candidate in checked
        if candidate not in universe
    ]
    total = len(checked)
    passed = total - len(findings)
    return total, passed, findings


def _mermaid_pass(
    universe: set[str], mermaid_labels: list[str]
) -> tuple[int, int, list[AuditFinding]]:
    """Flag mermaid node labels not in the entity universe (orphan lens).

    *mermaid_labels* is the caller's once-parsed list (shared with _entity_pass, #739).
    """
    labels = [lbl for lbl in mermaid_labels if not _is_structural(lbl)]
    findings = [
        AuditFinding(
            kind="mermaid_node_orphan",
            message=f"mermaid node {label!r} not found in analysis",
            affected_path="",
            severity="high",
        )
        for label in labels
        if label not in universe
    ]
    total = len(labels)
    passed = total - len(findings)
    return total, passed, findings


def _section_pass(playbook_md: str, expected_sections: int) -> tuple[int, int, list[AuditFinding]]:
    """Assert ``## 1.`` … ``## {expected_sections}.`` headings are all present."""
    findings: list[AuditFinding] = []
    present = 0
    for n in range(1, expected_sections + 1):
        if re.search(rf"^##\s+{n}\.", playbook_md, flags=re.MULTILINE):
            present += 1
        else:
            findings.append(
                AuditFinding(
                    kind="section_missing",
                    message=f"section {n} missing",
                    affected_path="",
                    severity="medium",
                )
            )
    return expected_sections, present, findings


#: A standalone ``basename.ext:line`` reference in prose — one NOT already
#: prefixed by a directory (the negative lookbehind rejects a preceding path
#: char or slash, so the basename inside ``billing/engine.py:42`` is left alone).
_BARE_BASENAME_REF_RE = re.compile(r"(?<![\w./\-])(?P<path>[\w.\-]+\.\w+):(?P<line>\d+)")


def _citation_basename_map(citations: list[str]) -> dict[str, str]:
    """Map each citation's basename → its full repo-relative path (first wins).

    The ``citations[]`` array entries are the section's validated grounding
    (``path:line`` with a directory-qualified path). Their basenames anchor the
    resolution of any bare-basename ref the harness wrote inline in the body.
    """
    out: dict[str, str] = {}
    for entry in citations:
        path = entry.rsplit(":", 1)[0]
        base = path.rsplit("/", 1)[-1]
        if base != path:  # only directory-qualified citations anchor a basename
            out.setdefault(base, path)
    return out


def _resolve_body_basenames(body: str, basename_map: dict[str, str]) -> str:
    """Rewrite bare ``basename.ext:line`` body refs to the matching citation path.

    The Phase D refcheck resolves every ``path:line`` against ``repo_root``, so a
    citation written in the section ``body`` by **basename** (e.g.
    ``fn_compute_tiered_rate.sql:13``) was flagged ``file_not_found`` even when the
    structured ``citations[]`` array cited the same file by its full path and
    resolved cleanly — the body and the array were refchecked with inconsistent
    path resolution (#949). Rewriting a body basename to its declared citation
    path makes the two consistent. Refs that already carry a directory, or whose
    basename matches no citation, are left untouched (still refchecked normally,
    so a genuinely hallucinated path is still flagged).
    """
    if not basename_map:
        return body

    def _sub(match: re.Match[str]) -> str:
        full = basename_map.get(match.group("path"))
        return f"{full}:{match.group('line')}" if full else match.group(0)

    return _BARE_BASENAME_REF_RE.sub(_sub, body)


def _synthesis_citation_pass(
    synthesis: SynthesisJudgments,
    repo_root: Path,
) -> tuple[int, int, list[AuditFinding]]:
    """Refcheck every ``path:line`` citation in harness synthesis sections.

    For each section in *synthesis*, both the explicit ``citations`` list entries
    and any inline ``path:line`` refs found in the ``body`` text are validated
    against the real tree using the same Layer-1 refcheck engine as
    :func:`_citation_pass`. Broken refs become ``synthesis_citation_unresolved``
    findings naming the section number and the bad ref. Counts are folded into
    the shared accuracy fraction — not tracked separately.

    *synthesis* is assumed to have been validated by the pydantic model so every
    ``citations`` entry already satisfies ``path:line`` format; the refcheck
    layer additionally verifies disk existence and line range.

    Inline ``path:line`` refs in the *body* written by **basename** are resolved
    against the section's ``citations[]`` before refcheck, so a citation that
    resolves in the structured array does not spuriously fail when the same file
    is mentioned by basename in the prose (#949).
    """
    # Build a synthetic pseudo-document: one line per citations entry
    # (prefixed by a fake path-with-extension so _REF_RE matches) plus
    # the raw body text. The refcheck engine's _REF_RE requires a file
    # extension immediately before the colon, which citations always have
    # (enforced by SynthesisSection._validate_citations). Body text may
    # contain inline refs in the same format — the engine extracts them
    # all naturally.
    findings: list[AuditFinding] = []
    total = 0
    passed = 0

    for section_num, section in synthesis.sections.items():
        # Compose a text block for this section: explicit citations first,
        # then the body. The refcheck engine scans every line for _REF_RE.
        # Resolve any bare-basename ref in the body against the section's
        # citations[] so prose and the structured array refcheck consistently
        # (#949).
        basename_map = _citation_basename_map(section.citations)
        resolved_body = _resolve_body_basenames(section.body, basename_map)
        lines = list(section.citations) + resolved_body.splitlines()
        pseudo_text = "\n".join(lines)

        report = refcheck_check(pseudo_text, repo_root)
        total += report.total_refs
        for broken in report.broken:
            findings.append(
                AuditFinding(
                    kind="synthesis_citation_unresolved",
                    message=(
                        f"synthesis section {section_num}: "
                        f"unresolvable citation {broken.ref!r} ({broken.reason})"
                    ),
                    affected_path=broken.path,
                    severity="high",
                )
            )
        passed += report.total_refs - len(report.broken)

    return total, passed, findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit(
    playbook_md: str,
    *,
    analysis: AnalysisDigest,
    repo_root: Path,
    evidence: Sequence[EvidenceRow] | None = None,
    expected_sections: int = 16,
    synthesis: SynthesisJudgments | None = None,
) -> AuditReport:
    """Audit ``playbook_md`` against ``analysis`` + the evidence ledger.

    Runs the four deterministic check families (citation / entity / mermaid /
    section) and, when *synthesis* is provided, an additional fifth family
    (synthesis_citation_unresolved). All families contribute to a single
    shared accuracy fraction. No LLM, no re-read pass: the engine verifies,
    the model fixes only the flagged findings.

    Args:
        playbook_md: the rendered Phase C ``PLAYBOOK.md`` text.
        analysis: the Phase B :class:`AnalysisDigest` (the entity universe).
        repo_root: the root the ``path:line`` citations resolve against.
        evidence: the optional Phase B evidence ledger. Accepted for the
            stable Phase D signature; the deterministic citation pass derives
            its checks from the playbook + tree directly, so the ledger is not
            consulted here (reserved for a future cross-reference pass).
        expected_sections: the canonical section count (16 for the M5 spine).
        synthesis: optional harness synthesis judgments (#862). When provided,
            every ``path:line`` citation in each section's ``citations`` list
            and inline refs found in ``body`` are refchecked against
            ``repo_root`` using the same Layer-1 engine. Broken refs become
            ``synthesis_citation_unresolved`` findings folded into the shared
            accuracy fraction. When ``None``, behaviour is byte-identical to
            the four-family pre-#862 result.

    Returns:
        An :class:`AuditReport` whose ``accuracy`` is ``passed_checks /
        total_checks`` (``1.0`` when there is nothing to check) and whose
        ``findings`` are sorted deterministically by ``(kind, message)``.
    """
    del evidence  # reserved — see docstring
    universe = _entity_universe(analysis)

    total = 0
    passed = 0
    findings: list[AuditFinding] = []
    mermaid_labels = _mermaid_labels(playbook_md)  # parse once, share across passes (#739)
    families: list[tuple[int, int, list[AuditFinding]]] = [
        _citation_pass(playbook_md, repo_root),
        _entity_pass(playbook_md, universe, mermaid_labels),
        _mermaid_pass(universe, mermaid_labels),
        _section_pass(playbook_md, expected_sections),
    ]
    if synthesis is not None:
        families.append(_synthesis_citation_pass(synthesis, repo_root))
    for fam_total, fam_passed, fam_findings in families:
        total += fam_total
        passed += fam_passed
        findings.extend(fam_findings)

    findings.sort(key=lambda f: (f.kind, f.message))
    accuracy = 1.0 if total == 0 else passed / total
    return AuditReport(
        accuracy=accuracy,
        findings=findings,
        total_checks=total,
        passed_checks=passed,
    )


# ---------------------------------------------------------------------------
# Validation Report rendering
# ---------------------------------------------------------------------------


def validation_report_section(report: AuditReport) -> str:
    """Render the ``## Validation Report`` markdown section for ``report``.

    Emits the accuracy line, a summary count line, and (when there are
    findings) a Markdown table of every finding sorted by ``(kind, message)``;
    when clean it emits ``✅ All checks passed.`` instead.
    """
    lines: list[str] = [
        "## Validation Report",
        "",
        f"**Accuracy: {report.accuracy * 100:.1f}%**",
        "",
        f"{report.passed_checks} of {report.total_checks} checks passed.",
        "",
    ]
    if not report.findings:
        lines.append("✅ All checks passed.")
        return "\n".join(lines)

    lines.append("| Kind | Severity | Detail |")
    lines.append("| --- | --- | --- |")
    for finding in report.findings:
        detail = finding.message.replace("|", r"\|")
        lines.append(f"| {finding.kind} | {finding.severity} | {detail} |")
    return "\n".join(lines)


def append_validation_report(playbook_md: str, report: AuditReport) -> str:
    """Return ``playbook_md`` with the ``## Validation Report`` section appended."""
    return playbook_md.rstrip() + "\n\n" + validation_report_section(report) + "\n"


__all__ = [
    "AuditReport",
    "append_validation_report",
    "audit",
    "validation_report_section",
]
