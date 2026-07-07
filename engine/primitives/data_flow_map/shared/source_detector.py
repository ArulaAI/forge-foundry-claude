"""S60 /data-flow-map — Stage A: PII source detection.

Two-tier detection:
  A.1 — Deterministic: name-based heuristics against classification catalog.
         Python: AST-based detection (assignments, function params, class attrs,
                 SQLAlchemy Column, Pydantic field annotations).
         TS/Java: regex-based.
  A.2 — Ambiguity resolution: for field names ambiguous to the regex catalog
         (e.g. 'value', 'data', 'identifier'), the skill body judges each via the
         extract→judge→continue protocol (``build_prompt_items_for_ambiguous_fields``
         / ``apply_pii_judgments``).
"""

from __future__ import annotations

import ast
import re
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError

from primitives._shared.scan_exclusions import is_excluded as _is_excluded
from primitives.data_flow_map.shared.models import (
    PIISource,
)
from primitives.data_flow_map.shared.prompts import (
    STAGE_A2_SYSTEM_PROMPT,
    build_stage_a2_user_message,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from primitives._shared.protocol import PromptItem
    from primitives.data_flow_map.shared.classification_loader import ClassificationEntry

# Field names that are too ambiguous for regex-only classification
_AMBIGUOUS_FIELD_NAMES = frozenset(
    {
        "value",
        "data",
        "identifier",
        "id",
        "info",
        "detail",
        "content",
        "payload",
        "field",
        "attribute",
        "record",
        "entry",
    }
)


# ---------------------------------------------------------------------------
# Stage A.1 — Deterministic regex catalog match
# ---------------------------------------------------------------------------


def _match_catalog(
    field_name: str, catalog: list[ClassificationEntry]
) -> ClassificationEntry | None:
    """Return the first catalog entry whose pattern matches field_name."""
    for entry in catalog:
        if re.search(entry.pattern, field_name, re.IGNORECASE):
            return entry
    return None


def _is_ambiguous(field_name: str) -> bool:
    """Return True if field_name is in the known-ambiguous set."""
    return field_name.lower() in _AMBIGUOUS_FIELD_NAMES


# ---------------------------------------------------------------------------
# Python AST-based detection
# ---------------------------------------------------------------------------


def _scan_python_file(
    path: Path, catalog: list[ClassificationEntry]
) -> tuple[list[PIISource], list[tuple[str, int, str]]]:
    """Scan a Python file via AST for PII source candidates.

    Returns:
        (definite_sources, ambiguous) — ambiguous is list of (field_name, line, context_snippet).
    """
    try:
        source_code = path.read_text(encoding="utf-8")
        tree = ast.parse(source_code)
    except (SyntaxError, UnicodeDecodeError):
        return [], []

    lines = source_code.splitlines()
    definite: list[PIISource] = []
    ambiguous: list[tuple[str, int, str]] = []

    def _consider(field_name: str, line: int) -> None:
        """Check field_name against catalog; bucket into definite or ambiguous."""
        entry = _match_catalog(field_name, catalog)
        if entry is not None:
            definite.append(
                PIISource(
                    name=field_name,
                    file=str(path),
                    line=line,
                    classification=entry.classification,
                    category=entry.category,
                    severity=entry.severity,
                    confidence="regex",
                )
            )
        elif _is_ambiguous(field_name):
            # Grab a few lines of context around the detection
            start = max(0, line - 2)
            end = min(len(lines), line + 3)
            snippet = "\n".join(lines[start:end])
            ambiguous.append((field_name, line, snippet))

    for node in ast.walk(tree):
        # Variable assignment: x = ... (simple name on left)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    _consider(target.id, target.lineno)

        # Annotated assignment: x: SomeType = ...
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                _consider(node.target.id, node.target.lineno)

        # Function parameters
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in node.args.args + node.args.kwonlyargs:
                _consider(arg.arg, arg.lineno)

        # Class attribute / SQLAlchemy / Pydantic field
        elif isinstance(node, ast.ClassDef):
            for item in ast.walk(node):
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    _consider(item.target.id, item.target.lineno)
                # SQLAlchemy Column: email = Column(String)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            _consider(target.id, target.lineno)

    return definite, ambiguous


# ---------------------------------------------------------------------------
# TypeScript / Java regex-based detection
# ---------------------------------------------------------------------------

# Regex to capture field-like names from TS/Java
_TS_FIELD_RE = re.compile(
    r"""(?x)
    (?:
      (?:private|public|protected|readonly|let|const|var)\s+    # TS modifier
      (?P<ts_name>[a-zA-Z_]\w*)                                  # field name
    |
      (?P<ts_param>[a-zA-Z_]\w*)\s*:                             # param: type
    )
    """,
    re.VERBOSE,
)

_JAVA_FIELD_RE = re.compile(
    r"""(?x)
    (?:
      @Column[^;]*?\n\s*\w+\s+(?P<java_col>[a-zA-Z_]\w*)    # @Column annotation
    |
      @RequestParam[^)]*?\)\s+\w+\s+(?P<java_param>[a-zA-Z_]\w*)  # Spring param
    |
      @RequestBody[^)]*?\)\s+\w+\s+(?P<java_body>[a-zA-Z_]\w*)    # Spring body
    |
      private\s+\w+\s+(?P<java_field>[a-zA-Z_]\w*)\s*;            # private field
    )
    """,
    re.VERBOSE | re.DOTALL,
)


def _scan_ts_file(path: Path, catalog: list[ClassificationEntry]) -> list[PIISource]:
    """Scan a TypeScript file via regex for PII source candidates."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    found: list[PIISource] = []
    for i, line in enumerate(source.splitlines(), 1):
        for m in _TS_FIELD_RE.finditer(line):
            field_name = m.group("ts_name") or m.group("ts_param")
            if field_name and len(field_name) > 1:
                entry = _match_catalog(field_name, catalog)
                if entry:
                    found.append(
                        PIISource(
                            name=field_name,
                            file=str(path),
                            line=i,
                            classification=entry.classification,
                            category=entry.category,
                            severity=entry.severity,
                            confidence="regex",
                        )
                    )
    return found


def _scan_java_file(path: Path, catalog: list[ClassificationEntry]) -> list[PIISource]:
    """Scan a Java file via regex for PII source candidates."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    found: list[PIISource] = []
    for i, line in enumerate(source.splitlines(), 1):
        # Simple field pattern: type fieldName;
        m = re.search(r"\bprivate\s+\w+\s+(\w+)\s*;", line)
        if m:
            field_name = m.group(1)
            entry = _match_catalog(field_name, catalog)
            if entry:
                found.append(
                    PIISource(
                        name=field_name,
                        file=str(path),
                        line=i,
                        classification=entry.classification,
                        category=entry.category,
                        severity=entry.severity,
                        confidence="regex",
                    )
                )
        # @Column, @RequestParam, @RequestBody lines
        if "@Column" in line or "@RequestParam" in line or "@RequestBody" in line:
            # try next line or same line for field name
            field_m = re.search(r"\b([a-zA-Z_]\w*)\s*;?\s*$", line)
            if field_m:
                field_name = field_m.group(1)
                if field_name not in {"Column", "RequestParam", "RequestBody", "String", "Integer"}:
                    entry = _match_catalog(field_name, catalog)
                    if entry:
                        found.append(
                            PIISource(
                                name=field_name,
                                file=str(path),
                                line=i,
                                classification=entry.classification,
                                category=entry.category,
                                severity=entry.severity,
                                confidence="regex",
                            )
                        )
    return found


# ---------------------------------------------------------------------------
# Stage A.2 — classification validation set
# ---------------------------------------------------------------------------

_VALID_CLASSIFICATIONS: set[str] = {"pii", "pci", "phi", "internal", "public"}


# ---------------------------------------------------------------------------
# Skill-first helpers (extract→judge→continue protocol)
# ---------------------------------------------------------------------------


def build_prompt_items_for_ambiguous_fields(
    ambiguous_batch: list[tuple[str, int, str, str]],
) -> list[PromptItem]:
    """Build PromptItems for ambiguous field names (Stage A.2 skill-first path).

    Args:
        ambiguous_batch: list of (field_name, line, file_str, snippet) tuples
            as collected by detect_sources_deterministic.

    Returns:
        One PromptItem per (field_name, file, line) triple, id = "pii:<field_name>:<file>:<line>".
        Deduplicates by field_name across files (one judgment per distinct name).
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    pii_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "is_pii": {"type": "boolean"},
            "classification": {"type": "string", "enum": sorted(_VALID_CLASSIFICATIONS)},
            "category": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        },
        "required": ["is_pii", "classification", "category", "severity"],
    }

    seen_names: set[str] = set()
    items: list[PromptItem] = []
    for field_name, _line, _file_str, snippet in ambiguous_batch:
        if field_name in seen_names:
            continue
        seen_names.add(field_name)
        user_msg = build_stage_a2_user_message(field_name, snippet)
        items.append(
            PromptItem(
                id=f"pii:{field_name}",
                system=STAGE_A2_SYSTEM_PROMPT,
                user=user_msg,
                response_format=ResponseFormat(type="json", schema=pii_schema),
            )
        )
    return items


def apply_pii_judgments(
    ambiguous_batch: list[tuple[str, int, str, str]],
    judgment_map: Mapping[str, object],
) -> list[PIISource]:
    """Apply in-session judgments from the SKILL.md body to ambiguous fields.

    Mirrors the anti-hallucination guard in ``_resolve_ambiguous_field`` —
    any item whose classification is not in ``_VALID_CLASSIFICATIONS`` is dropped
    with a stderr warning.

    Args:
        ambiguous_batch: list of (field_name, line, file_str, snippet) tuples.
        judgment_map: mapping of item id → response dict (from JudgmentEnvelope.by_id()).

    Returns:
        List of PIISource objects for fields confirmed as PII by the model.
    """

    resolved: list[PIISource] = []
    seen_names: set[str] = set()

    for field_name, line, file_str, _snippet in ambiguous_batch:
        item_id = f"pii:{field_name}"
        if field_name in seen_names:
            continue
        seen_names.add(field_name)

        raw_response = judgment_map.get(item_id)
        if raw_response is None:
            print(
                f"[data-flow-map] WARNING: no judgment for ambiguous field {field_name!r} "
                f"(id={item_id!r}) — skipping",
                file=sys.stderr,
            )
            continue

        if not isinstance(raw_response, dict):
            print(
                f"[data-flow-map] WARNING: judgment response for {item_id!r} "
                "is not a dict — skipping",
                file=sys.stderr,
            )
            continue

        if not raw_response.get("is_pii", False):
            continue

        classification = raw_response.get("classification", "")
        if classification not in _VALID_CLASSIFICATIONS:
            print(
                f"[data-flow-map] WARNING: Stage A.2 judgment returned unknown classification "
                f"{classification!r} for {field_name!r} — dropping",
                file=sys.stderr,
            )
            continue

        category = raw_response.get("category", "identity")
        severity = raw_response.get("severity", "medium")

        valid_categories = {
            "identity",
            "contact",
            "government_id",
            "payment_account",
            "payment_auth",
            "medical",
            "financial_account",
            "address",
            "credential",
            "biometric",
            "behavioral",
            "location",
        }
        valid_severities = {"low", "medium", "high", "critical"}
        if category not in valid_categories:
            category = "identity"
        if severity not in valid_severities:
            severity = "medium"

        try:
            resolved.append(
                PIISource(
                    name=field_name,
                    file=file_str,
                    line=line,
                    classification=classification,
                    category=category,
                    severity=severity,
                    confidence="llm",
                )
            )
        except ValidationError as exc:
            print(
                f"[data-flow-map] WARNING: Stage A.2 PIISource construction failed "
                f"for {field_name!r}: {exc}",
                file=sys.stderr,
            )

    return resolved


def detect_sources_deterministic(
    code_path: Path,
    catalog: list[ClassificationEntry],
    language: str = "auto",
    *,
    include_vendored: bool = False,
) -> tuple[list[PIISource], list[tuple[str, int, str, str]]]:
    """Stage A.1 only — no LLM calls.

    Returns:
        (definite_sources, ambiguous_batch) where ambiguous_batch is a list of
        (field_name, line, file_str, snippet) tuples for Stage A.2 prompts.
    """
    sources: list[PIISource] = []
    ambiguous_batch: list[tuple[str, int, str, str]] = []

    files: list[Path]
    files = (
        [code_path]
        if code_path.is_file()
        else [
            p
            for p in code_path.rglob("*")
            if not p.is_symlink()
            and not _is_excluded(p, code_path, include_vendored=include_vendored)
        ]
    )

    for file_path in sorted(files):
        if not file_path.is_file():
            continue

        suffix = file_path.suffix.lower()

        if language == "auto":
            if suffix == ".py":
                lang = "python"
            elif suffix in {".ts", ".tsx"}:
                lang = "ts"
            elif suffix == ".java":
                lang = "java"
            else:
                continue
        elif suffix == ".py" and language == "python":
            lang = "python"
        elif suffix in {".ts", ".tsx"} and language == "ts":
            lang = "ts"
        elif suffix == ".java" and language == "java":
            lang = "java"
        else:
            continue

        if lang == "python":
            definite, ambi = _scan_python_file(file_path, catalog)
            sources.extend(definite)
            for field_name, line, snippet in ambi:
                ambiguous_batch.append((field_name, line, str(file_path), snippet))
        elif lang == "ts":
            sources.extend(_scan_ts_file(file_path, catalog))
        elif lang == "java":
            sources.extend(_scan_java_file(file_path, catalog))

    # Deduplicate definite sources
    seen: set[tuple[str, str, int]] = set()
    result: list[PIISource] = []
    for s in sources:
        key = (s.name, s.file, s.line)
        if key not in seen:
            seen.add(key)
            result.append(s)

    return result, ambiguous_batch
