"""C5 /spec-validate — Stage E: scope drift detector.

Builds the skill-first drift prompt (the implementation summary of top-level
public functions/classes/endpoints) and applies the in-session judgment that
flags features NOT mentioned in the spec.

Anti-hallucination: file validation against the reviewed-files set (drop).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from primitives._shared.protocol import PromptItem, ResponseFormat
from primitives.spec_validate.shared.code_element_searcher import _VALID_KINDS
from primitives.spec_validate.shared.models import ScopeDriftItem
from primitives.spec_validate.shared.prompts import STAGE_E_SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Implementation summary builder
# ---------------------------------------------------------------------------


def _extract_public_symbols(code_path: Path, reviewed_files: set[str]) -> list[dict[str, object]]:
    """Extract top-level public symbols from the codebase.

    Args:
        code_path: Root of the codebase.
        reviewed_files: Set of relative file paths to scan.

    Returns:
        List of dicts: {file, line, name, kind} for public symbols.
    """
    symbols: list[dict[str, object]] = []

    for rel_file in sorted(reviewed_files)[:50]:  # limit
        abs_path = code_path / rel_file if not Path(rel_file).is_absolute() else Path(rel_file)

        if abs_path.suffix == ".py" and abs_path.is_file():
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not node.name.startswith("_"):
                            symbols.append(
                                {
                                    "file": rel_file,
                                    "line": node.lineno,
                                    "name": node.name,
                                    "kind": "function",
                                }
                            )
                    elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                        symbols.append(
                            {
                                "file": rel_file,
                                "line": node.lineno,
                                "name": node.name,
                                "kind": "class",
                            }
                        )
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue

    return symbols[:100]  # limit total symbols


def _build_implementation_summary(
    code_path: Path, reviewed_files: set[str]
) -> tuple[str, list[dict[str, object]]]:
    """Build the implementation summary for Stage E LLM call.

    Args:
        code_path: Root of the codebase.
        reviewed_files: Set of relative file paths.

    Returns:
        Tuple of (summary_text, symbols_list)
    """
    symbols = _extract_public_symbols(code_path, reviewed_files)

    lines: list[str] = ["=== Implementation Public API ==="]
    for sym in symbols:
        lines.append(f"  {sym['file']}:{sym['line']} - {sym['name']} ({sym['kind']})")

    return "\n".join(lines), symbols


# ---------------------------------------------------------------------------
# Anti-hallucination for scope drift
# ---------------------------------------------------------------------------


def _validate_scope_drift_items(
    raw_items: list[dict[str, object]],
    reviewed_files: set[str],
) -> list[ScopeDriftItem]:
    """Validate LLM-returned scope drift items.

    Anti-hallucination: file must be in reviewed_files set (DROP if not).

    Args:
        raw_items: Raw dicts from LLM response.
        reviewed_files: Set of valid relative file paths.

    Returns:
        List of validated ScopeDriftItem instances.
    """
    validated: list[ScopeDriftItem] = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        file_val = item.get("file", "")
        line_val = item.get("line", 1)
        name_val = item.get("name", "")
        kind_val = item.get("kind", "other")
        desc_val = item.get("description", "")

        if not isinstance(file_val, str):
            continue

        line_int = int(line_val) if isinstance(line_val, (int, float)) and line_val > 0 else 1

        # Anti-hallucination: file validation (DROP)
        if file_val not in reviewed_files:
            basename = Path(file_val).name
            matched = next(
                (f for f in reviewed_files if Path(f).name == basename),
                None,
            )
            if matched is None:
                print(
                    f"[spec-validate] scope-drift: dropping unknown file {file_val!r}",
                    file=sys.stderr,
                )
                continue
            file_val = matched

        # Anti-hallucination: kind Literal validation (CLEAR to 'other' if invalid),
        # mirroring code_element_searcher so an out-of-vocabulary kind can't break
        # the typed ScopeDriftItem.
        if not isinstance(kind_val, str) or kind_val not in _VALID_KINDS:
            print(
                f"[spec-validate] scope-drift: invalid kind {kind_val!r}; clearing to 'other'",
                file=sys.stderr,
            )
            kind_val = "other"

        validated.append(
            ScopeDriftItem(
                file=file_val,
                line=line_int,
                name=str(name_val),
                kind=kind_val,  # type: ignore[arg-type]
                description=str(desc_val),
            )
        )

    return validated


# ---------------------------------------------------------------------------
# Skill-first protocol helpers (§3 of harness-elimination spec)
# ---------------------------------------------------------------------------


def build_prompt_item_for_drift(
    spec_text: str,
    code_path: Path,
    reviewed_files: set[str],
) -> PromptItem:
    """Build a :class:`~primitives._shared.protocol.PromptItem` for scope drift detection.

    Item id is ``"drift:scope"``.

    Args:
        spec_text: Full text of the spec Markdown file.
        code_path: Root of the codebase.
        reviewed_files: Set of relative file paths.

    Returns:
        A :class:`~primitives._shared.protocol.PromptItem` ready for
        :class:`~primitives._shared.protocol.PromptEnvelope`.
    """
    impl_summary, _symbols = _build_implementation_summary(code_path, reviewed_files)
    user_message = f"=== Spec Text ===\n{spec_text[:4000]}\n\n{impl_summary}"

    drift_item_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "name": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": list(_VALID_KINDS),
            },
            "description": {"type": "string"},
        },
        "required": ["file", "line", "name", "kind", "description"],
    }
    drift_schema: dict[str, object] = {
        "type": "array",
        "items": drift_item_schema,
    }

    return PromptItem(
        id="drift:scope",
        system=STAGE_E_SYSTEM_PROMPT,
        user=user_message,
        response_format=ResponseFormat(type="json", schema=drift_schema),
    )


def apply_drift_judgment(
    response: object,
    reviewed_files: set[str],
) -> list[ScopeDriftItem]:
    """Apply a judgment response for the drift item.

    Applies the same anti-hallucination guard as the deterministic detector.

    Args:
        response: The raw response value from the judgment envelope
                  (expected to be a list of drift item dicts).
        reviewed_files: Set of valid relative file paths for anti-hallucination.

    Returns:
        List of validated ScopeDriftItem instances (empty on malformed input).
    """
    if not isinstance(response, list):
        print(
            "[spec-validate] WARNING: drift judgment response was not a list; returning []",
            file=sys.stderr,
        )
        return []

    return _validate_scope_drift_items(response, reviewed_files)
