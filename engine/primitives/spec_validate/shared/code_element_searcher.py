"""C5 /spec-validate — Stage B + C: code-element search and match classification.

Stage B: build one skill-first prompt per criterion, searching for implementing
code elements; the in-session judgment supplies the candidate elements.
Stage C: classify match status + 3-way anti-hallucination:
  1. file validation: file must be in the reviewed-files set (drop)
  2. code-element-exists: file must exist AND symbol must be in the file via grep (clear)
  3. kind Literal: kind must be one of the valid CodeElementKind values (clear)
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from primitives._shared.protocol import PromptItem, ResponseFormat
from primitives.spec_validate.shared.models import (
    AcceptanceTrace,
    CodeElementRef,
    Confidence,
    MatchStatus,
    Severity,
)
from primitives.spec_validate.shared.prompts import STAGE_B_SYSTEM_PROMPT

if TYPE_CHECKING:
    from primitives._shared.acceptance import AcceptanceCriterion

# ---------------------------------------------------------------------------
# Valid kind values (for anti-hallucination validation)
# ---------------------------------------------------------------------------

_VALID_KINDS: frozenset[str] = frozenset(
    ["function", "class", "method", "endpoint", "test", "constant", "other"]
)

_VALID_STATUSES: frozenset[str] = frozenset(
    ["satisfied", "partial", "unsatisfied", "not_applicable"]
)

_VALID_CONFIDENCES: frozenset[str] = frozenset(["low", "medium", "high"])

# Severity mapping for match status
_STATUS_TO_SEVERITY: dict[str, Severity] = {
    "satisfied": "low",
    "partial": "medium",
    "unsatisfied": "high",
    "not_applicable": "low",
}


# ---------------------------------------------------------------------------
# Anti-hallucination helpers
# ---------------------------------------------------------------------------


def symbol_in_file(file_path: Path, name: str) -> bool:
    """Return True if *name* appears as a top-level or class-level symbol in *file_path*.

    Strategy:
    1. Try AST parse for Python files (precise).
    2. Fall back to regex grep for non-Python or on parse error.

    Args:
        file_path: The file to search in.
        name: Symbol name to look for.

    Returns:
        True if the symbol exists in the file, False otherwise.
    """
    if not file_path.is_file():
        return False

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    if file_path.suffix == ".py":
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == name:
                        return True
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            return True
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == name
                ):
                    return True
            return False
        except SyntaxError:
            pass

    # Fallback: regex grep for any file type
    pattern = re.compile(
        r"(?:^|\s)(?:def|class|const|function|interface|type|let|var|export\s+(?:function|class|const|let|var))\s+"
        + re.escape(name)
        + r"\b",
        re.MULTILINE,
    )
    return bool(pattern.search(source)) or bool(
        re.search(r"\b" + re.escape(name) + r"\s*[=(:]", source)
    )


def _validate_code_elements(
    raw_elements: list[dict[str, object]],
    reviewed_files: set[str],
    code_path: Path,
) -> list[CodeElementRef]:
    """Apply 3-way anti-hallucination to raw LLM-returned code elements.

    1. File validation: file must be in reviewed_files set (DROP if not).
    2. Code-element-exists: symbol must be in the file via AST/grep (CLEAR name if not).
    3. Kind Literal: kind must be valid (CLEAR to 'other' if not).

    Args:
        raw_elements: List of dicts from LLM response.
        reviewed_files: Set of relative file paths in the codebase.
        code_path: Root of the codebase for resolving file paths.

    Returns:
        Validated list of CodeElementRef instances.
    """
    validated: list[CodeElementRef] = []

    for elem in raw_elements:
        if not isinstance(elem, dict):
            continue

        file_val = elem.get("file", "")
        line_val = elem.get("line", 1)
        name_val = elem.get("name", "")
        kind_val = elem.get("kind", "other")

        if not isinstance(file_val, str) or not isinstance(name_val, str):
            continue

        line_int = int(line_val) if isinstance(line_val, (int, float)) and line_val > 0 else 1

        # Anti-hallucination 1: File validation (DROP if file not in reviewed set)
        if file_val not in reviewed_files:
            # Try basename match
            basename = Path(file_val).name
            matched = next(
                (f for f in reviewed_files if Path(f).name == basename),
                None,
            )
            if matched is None:
                print(
                    f"[spec-validate] WARNING: LLM returned unknown file {file_val!r}; dropping",
                    file=sys.stderr,
                )
                continue
            file_val = matched

        # Anti-hallucination 2: Code-element-exists (CLEAR name if not found)
        abs_file = code_path / file_val if not Path(file_val).is_absolute() else Path(file_val)
        if name_val and not symbol_in_file(abs_file, name_val):
            print(
                f"[spec-validate] WARNING: symbol {name_val!r} not found in {file_val}; "
                "clearing name",
                file=sys.stderr,
            )
            name_val = ""

        # Anti-hallucination 3: Kind Literal validation (CLEAR to 'other' if invalid)
        if not isinstance(kind_val, str) or kind_val not in _VALID_KINDS:
            print(
                f"[spec-validate] WARNING: invalid kind {kind_val!r}; clearing to 'other'",
                file=sys.stderr,
            )
            kind_val = "other"

        validated.append(
            CodeElementRef(
                file=file_val,
                line=line_int,
                name=name_val,
                kind=kind_val,  # type: ignore[arg-type]
            )
        )

    return validated


# ---------------------------------------------------------------------------
# Stage B + C: search and classify
# ---------------------------------------------------------------------------


def _build_codebase_summary(code_path: Path, reviewed_files: set[str]) -> str:
    """Build a compact codebase summary for the LLM prompt.

    Lists files and their top-level public symbols (Python only; other files listed by name).

    Args:
        code_path: Root of the codebase.
        reviewed_files: Set of relative file paths.

    Returns:
        Multi-line string describing the codebase structure.
    """
    lines: list[str] = ["=== Codebase Summary ==="]

    for rel_file in sorted(reviewed_files)[:50]:  # limit to 50 files
        abs_path = code_path / rel_file if not Path(rel_file).is_absolute() else Path(rel_file)
        lines.append(f"\nFile: {rel_file}")

        if abs_path.suffix == ".py" and abs_path.is_file():
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
                symbols: list[str] = []
                for node in ast.walk(tree):
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ) and not node.name.startswith("_"):
                        symbols.append(f"  - {node.name} ({type(node).__name__})")
                if symbols:
                    lines.extend(symbols[:20])  # limit per file
            except (SyntaxError, OSError, UnicodeDecodeError):
                lines.append("  (parse error)")

    return "\n".join(lines)


def classify_match(
    criterion: AcceptanceCriterion,
    raw_elements: list[dict[str, object]],
    reviewed_files: set[str],
    code_path: Path,
    raw_status: str = "unsatisfied",
    raw_confidence: str = "medium",
    rationale: str = "",
) -> AcceptanceTrace:
    """Classify a match from pre-parsed LLM output (testable without LLM call).

    This is the Stage C logic extracted for direct testing.

    Args:
        criterion: The acceptance criterion.
        raw_elements: Raw code element dicts from LLM.
        reviewed_files: Reviewed file paths for anti-hallucination.
        code_path: Code root.
        raw_status: Raw status string from LLM.
        raw_confidence: Raw confidence string from LLM.
        rationale: Rationale from LLM.

    Returns:
        AcceptanceTrace with validated data.
    """
    if raw_status not in _VALID_STATUSES:
        raw_status = "unsatisfied"
    if raw_confidence not in _VALID_CONFIDENCES:
        raw_confidence = "low"

    status: MatchStatus = raw_status  # type: ignore[assignment]
    confidence: Confidence = raw_confidence  # type: ignore[assignment]

    code_elements = _validate_code_elements(raw_elements, reviewed_files, code_path)

    if status in ("satisfied", "partial") and not code_elements:
        status = "unsatisfied"
        rationale = f"{rationale} (downgraded: no valid code elements after anti-hallucination)"

    severity: Severity = _STATUS_TO_SEVERITY.get(status, "medium")

    return AcceptanceTrace(
        criterion_id=criterion.id,
        criterion_text=criterion.text,
        status=status,
        code_elements=code_elements,
        rationale=rationale,
        severity=severity,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Skill-first protocol helpers (§3 of harness-elimination spec)
# ---------------------------------------------------------------------------


def build_prompt_items_for_criteria(
    criteria: list[AcceptanceCriterion],
    codebase_summary: str,
) -> list[PromptItem]:
    """Build :class:`~primitives._shared.protocol.PromptItem` objects for each criterion.

    Each item id is ``"element:<criterion.id>"``.

    Args:
        criteria: Acceptance criteria parsed from the spec.
        codebase_summary: Pre-built codebase summary string.

    Returns:
        List of :class:`~primitives._shared.protocol.PromptItem` instances ready for
        :class:`~primitives._shared.protocol.PromptEnvelope`.
    """
    element_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "code_elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "name": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": list(_VALID_KINDS),
                        },
                    },
                    "required": ["file", "line", "name", "kind"],
                },
            },
            "status": {
                "type": "string",
                "enum": list(_VALID_STATUSES),
            },
            "rationale": {"type": "string"},
            "severity": {"type": "string"},
            "confidence": {
                "type": "string",
                "enum": list(_VALID_CONFIDENCES),
            },
        },
        "required": ["code_elements", "status", "rationale", "confidence"],
    }

    items: list[PromptItem] = []
    for criterion in criteria:
        user_message = (
            f"=== Acceptance Criterion ===\n"
            f"ID: {criterion.id}\n"
            f"Text: {criterion.text}\n\n"
            f"{codebase_summary}"
        )
        items.append(
            PromptItem(
                id=f"element:{criterion.id}",
                system=STAGE_B_SYSTEM_PROMPT,
                user=user_message,
                response_format=ResponseFormat(type="json", schema=element_schema),
            )
        )
    return items


def apply_element_judgments(
    criteria: list[AcceptanceCriterion],
    judgment_map: dict[str, object],
    reviewed_files: set[str],
    code_path: Path,
) -> list[AcceptanceTrace]:
    """Apply judgment responses for element items to produce AcceptanceTrace list.

    Missing ids are treated as unsatisfied.  For each criterion, looks up
    ``"element:<criterion.id>"`` in *judgment_map* and applies the same
    anti-hallucination logic as :func:`search_code_element`.

    Args:
        criteria: Acceptance criteria parsed from the spec.
        judgment_map: Dict keyed by item id (e.g. ``{"element:AC-1": {...}}``)
                      where values are the raw JSON response dicts.
        reviewed_files: Set of relative file paths for anti-hallucination.
        code_path: Root of the codebase for resolving file paths.

    Returns:
        List of AcceptanceTrace instances, one per criterion.
    """
    traces: list[AcceptanceTrace] = []

    for criterion in criteria:
        item_id = f"element:{criterion.id}"
        response = judgment_map.get(item_id)

        if response is None:
            print(
                f"[spec-validate] WARNING: no judgment for {item_id!r}; treating as unsatisfied",
                file=sys.stderr,
            )
            traces.append(
                AcceptanceTrace(
                    criterion_id=criterion.id,
                    criterion_text=criterion.text,
                    status="unsatisfied",
                    code_elements=[],
                    rationale="No judgment provided.",
                    severity="high",
                    confidence="low",
                )
            )
            continue

        if not isinstance(response, dict):
            print(
                f"[spec-validate] WARNING: judgment for {item_id!r} is not a dict; "
                "treating as unsatisfied",
                file=sys.stderr,
            )
            traces.append(
                AcceptanceTrace(
                    criterion_id=criterion.id,
                    criterion_text=criterion.text,
                    status="unsatisfied",
                    code_elements=[],
                    rationale="Malformed judgment response.",
                    severity="high",
                    confidence="low",
                )
            )
            continue

        # Reuse the same parsing logic as search_code_element
        status_raw = response.get("status", "unsatisfied")
        if not isinstance(status_raw, str) or status_raw not in _VALID_STATUSES:
            status_raw = "unsatisfied"
        status: MatchStatus = status_raw  # type: ignore[assignment]

        confidence_raw = response.get("confidence", "low")
        if not isinstance(confidence_raw, str) or confidence_raw not in _VALID_CONFIDENCES:
            confidence_raw = "low"
        confidence: Confidence = confidence_raw  # type: ignore[assignment]

        rationale = response.get("rationale", "")
        if not isinstance(rationale, str):
            rationale = ""

        raw_elements = response.get("code_elements", [])
        if not isinstance(raw_elements, list):
            raw_elements = []

        code_elements = _validate_code_elements(raw_elements, reviewed_files, code_path)

        if status in ("satisfied", "partial") and not code_elements:
            status = "unsatisfied"
            rationale = f"{rationale} (downgraded: no valid code elements after anti-hallucination)"

        severity: Severity = _STATUS_TO_SEVERITY.get(status, "medium")

        traces.append(
            AcceptanceTrace(
                criterion_id=criterion.id,
                criterion_text=criterion.text,
                status=status,
                code_elements=code_elements,
                rationale=rationale,
                severity=severity,
                confidence=confidence,
            )
        )

    return traces


def enumerate_reviewed_files(code_path: Path) -> set[str]:
    """Return the set of relative file paths in the codebase.

    Args:
        code_path: Directory to scan.

    Returns:
        Set of relative paths (strings) from code_path.
    """
    if not code_path.is_dir():
        if code_path.is_file():
            return {code_path.name}
        return set()

    files: set[str] = set()
    for p in code_path.rglob("*"):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(code_path).parts):
            files.add(str(p.relative_to(code_path)))
    return files
