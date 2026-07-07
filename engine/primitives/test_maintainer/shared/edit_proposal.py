from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

JSON_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class ImpactSummary:
    """Snapshot of the impact-engine run included in every report.

    Included even when the proposal list is empty so an operator can
    distinguish "no impact found" from "engine was skipped (wrong mode /
    empty diff)."
    """

    mode: str  # "regex" | "llm"
    diff_against: str  # the ref used (e.g. "HEAD~1", "origin/main")
    changed_files: int  # total files returned by git diff (all types)
    source_changed: int  # subset filtered to source (non-test) extensions
    impacted_tests: int  # count of (source, test) pairs found
    coverage_measured: bool  # True when a live coverage map was available
    coverage_source: str  # "measured" | "static"

    @classmethod
    def empty(
        cls,
        mode: str,
        diff_against: str,
        changed_files: int = 0,
        source_changed: int = 0,
    ) -> ImpactSummary:
        """Construct a summary for the zero-proposals case."""
        return cls(
            mode=mode,
            diff_against=diff_against,
            changed_files=changed_files,
            source_changed=source_changed,
            impacted_tests=0,
            coverage_measured=False,
            coverage_source="static",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "diff_against": self.diff_against,
            "changed_files": self.changed_files,
            "source_changed": self.source_changed,
            "impacted_tests": self.impacted_tests,
            "coverage_measured": self.coverage_measured,
            "coverage_source": self.coverage_source,
        }

    def to_markdown(self) -> str:
        """Render a compact impact-map section for the report."""
        lines = [
            "## Impact summary",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| mode | `{self.mode}` |",
            f"| diff_against | `{self.diff_against}` |",
            f"| changed_files | {self.changed_files} |",
            f"| source_changed | {self.source_changed} |",
            f"| impacted_tests | {self.impacted_tests} |",
            f"| coverage_source | {self.coverage_source} |",
            f"| coverage_measured | {self.coverage_measured} |",
            "",
        ]
        return "\n".join(lines)


class Verdict(Enum):
    UPDATE = "update"  # modify existing test
    ADD = "add"  # propose net-new test
    DELETE = "delete"  # remove the test


@dataclass(frozen=True)
class EditProposal:
    test_file: Path  # path to the affected test
    verdict: Verdict
    before_content: str  # current test file content; "" for ADD
    after_content: str  # proposed new content; "" for DELETE
    reasoning: str  # 1-3 sentence rationale
    impact_classes: frozenset[str]  # str values from ImpactClass.value
    warnings: tuple[str, ...] = ()  # FM1-FM4 audit warnings (non-empty = audited with findings)


def render_unified_diff(proposal: EditProposal) -> str:
    """Return a unified diff string (difflib.unified_diff) of before → after."""
    before_lines = proposal.before_content.splitlines(keepends=True)
    after_lines = proposal.after_content.splitlines(keepends=True)
    label = str(proposal.test_file)
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
        lineterm="",
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in diff)


def render_markdown_report(
    proposals: list[EditProposal],
    impact_summary: ImpactSummary | None = None,
) -> str:
    """Render a complete markdown report.

    When *impact_summary* is provided it is appended as an ``## Impact
    summary`` table so operators can distinguish "no proposals because
    nothing changed" from "engine was skipped or diffed against 0 files."

    Format:
        # Proposed test edits

        Summary: N tests affected; A updates, B adds, C deletes.

        ## test_file_1.py (verdict: UPDATE, impacts: signature, behavior)
        Reasoning: ...

        ```diff
        <unified diff>
        ```

        ## Impact summary
        | Field | Value |
        ...
    """
    if not proposals:
        parts = ["# Proposed test edits", "", "No proposals.", ""]
        if impact_summary is not None:
            parts.append(impact_summary.to_markdown())
        return "\n".join(parts)

    updates = sum(1 for p in proposals if p.verdict == Verdict.UPDATE)
    adds = sum(1 for p in proposals if p.verdict == Verdict.ADD)
    deletes = sum(1 for p in proposals if p.verdict == Verdict.DELETE)

    lines: list[str] = [
        "# Proposed test edits",
        "",
        f"Summary: {len(proposals)} tests affected;"
        f" {updates} updates, {adds} adds, {deletes} deletes.",
        "",
    ]
    for p in proposals:
        impacts = ", ".join(sorted(p.impact_classes)) or "none"
        lines.append(f"## {p.test_file} (verdict: {p.verdict.value.upper()}, impacts: {impacts})")
        lines.append("")
        lines.append(f"Reasoning: {p.reasoning}")
        lines.append("")
        if p.warnings:
            lines.append("**Audit warnings (FM1-FM4):**")
            lines.append("")
            for w in p.warnings:
                lines.append(f"- {w}")
            lines.append("")
        lines.append("```diff")
        diff = render_unified_diff(p).rstrip()
        lines.append(diff)
        lines.append("```")
        lines.append("")
    if impact_summary is not None:
        lines.append(impact_summary.to_markdown())
    return "\n".join(lines)


def to_json(
    proposals: list[EditProposal],
    impact_summary: ImpactSummary | None = None,
) -> str:
    """Schema-versioned JSON wrapper: {schema_version: '1.0', proposals: [...]}.

    When *impact_summary* is provided, it is included as ``impact_summary``
    in the top-level object so the caller can distinguish "no proposals
    because nothing changed" from "engine was skipped."

    Each proposal serialized with all fields; Path → str, frozenset → sorted list.
    """
    data: dict[str, object] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "proposals": [
            {
                "test_file": str(p.test_file),
                "verdict": p.verdict.value,
                "before_content": p.before_content,
                "after_content": p.after_content,
                "reasoning": p.reasoning,
                "impact_classes": sorted(p.impact_classes),
                "warnings": list(p.warnings),
            }
            for p in proposals
        ],
    }
    if impact_summary is not None:
        data["impact_summary"] = impact_summary.to_dict()
    return json.dumps(data, indent=2, ensure_ascii=False)
