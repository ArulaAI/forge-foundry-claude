from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_ENTITY_RE = re.compile(r"(?:class|interface)\s+(\w+)")

_ANNOTATION_RE = re.compile(
    r"@(NotNull|NotEmpty|NotBlank|Size|Min|Max|Pattern|Email|Positive|"
    r"PositiveOrZero|Negative|NegativeOrZero|DecimalMin|DecimalMax|"
    r"Digits|Future|FutureOrPresent|Past|PastOrPresent|AssertTrue|AssertFalse|"
    r"Length|Range|URL|CreditCardNumber|Valid)\s*(?:\(([^)]*)\))?"
)

# Field/method declaration line: optional access modifier, type, name
_DECL_RE = re.compile(
    r"^\s*(?:(?:private|protected|public|static|final|transient|volatile)\s+)*"
    r"\S+\s+(\w+)\s*[;=({\[]"
)
# Method declaration (for cross-field @AssertTrue/@AssertFalse)
_METHOD_DECL_RE = re.compile(
    r"^\s*(?:(?:private|protected|public|static|final)\s+)*"
    r"\S+\s+(\w+)\s*\("
)

_PARAMS_RE = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([\d.]+))')

_CROSS_FIELD = frozenset(("AssertTrue", "AssertFalse"))

_QUICK_CHECK = frozenset(
    (
        "@NotNull",
        "@Size",
        "@Pattern",
        "@Min",
        "@Max",
        "@Valid",
        "@AssertTrue",
        "@AssertFalse",
        "@NotEmpty",
        "@NotBlank",
        "@Email",
        "@DecimalMin",
        "@DecimalMax",
    )
)


def _parse_params(args_str: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for m in _PARAMS_RE.finditer(args_str or ""):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        if key not in ("message", "groups", "payload"):
            params[key] = val
    return params


def parse(file_path: Path, content: str) -> dict[str, list[Any]]:
    """Returns {} if file is not .java or has no Bean Validation annotations."""
    if Path(file_path).suffix.lower() != ".java":
        return {}
    if not any(sig in content for sig in _QUICK_CHECK):
        return {}

    entity_m = _ENTITY_RE.search(content)
    entity = entity_m.group(1) if entity_m else str(Path(file_path).stem)

    lines = content.split("\n")
    validation_rules: list[dict[str, Any]] = []

    # Accumulate consecutive annotation lines; when we hit a field/method decl, flush
    pending: list[tuple[str, str, str]] = []  # (constraint, args_str, raw_line)

    for line in lines:
        stripped = line.strip()

        # Check if this line has one or more constraint annotations
        ann_matches = list(_ANNOTATION_RE.finditer(stripped))
        if ann_matches:
            for m in ann_matches:
                constraint = m.group(1)
                args_str = m.group(2) or ""
                pending.append((constraint, args_str, stripped))
            continue

        if not pending:
            continue

        # Try to match a field or method declaration
        field_m = _DECL_RE.match(line)
        meth_m = _METHOD_DECL_RE.match(line)
        field_name: str | None = None
        if field_m:
            field_name = field_m.group(1)
        elif meth_m:
            field_name = meth_m.group(1)

        if field_name:
            for constraint, args_str, _ in pending:
                cross_field = constraint in _CROSS_FIELD

                msg_m = re.search(r'message\s*=\s*"([^"]+)"', args_str)
                message = msg_m.group(1) if msg_m else ""
                error_i18n_key = ""
                if message.startswith("{") and message.endswith("}"):
                    error_i18n_key = message[1:-1]
                    message = ""

                params = _parse_params(args_str)

                validation_rules.append(
                    {
                        "entity": entity,
                        "field": field_name,
                        "constraint": constraint,
                        "params": params,
                        "message": message,
                        "error_i18n_key": error_i18n_key,
                        "cross_field": cross_field,
                    }
                )
            pending = []
        elif stripped and not stripped.startswith("@") and not stripped.startswith("//"):
            # Non-blank, non-annotation, non-comment line that isn't a decl — flush
            pending = []

    if not validation_rules:
        return {}
    return {"validation_rules": validation_rules}
