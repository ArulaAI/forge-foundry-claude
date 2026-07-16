from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_ENTITY_SIGNAL = ("@Entity", "@Table")

_CLASS_RE = re.compile(r'@Table\s*\([^)]*name\s*=\s*"([^"]+)"')
_CLASS_NAME_RE = re.compile(r"class\s+(\w+)")

# @Column annotation
_COLUMN_RE = re.compile(
    r"@Column\s*\(([^)]*)\)\s*(?:@\w+[^)]*\)\s*)*"
    r"(?:private|protected)\s+\S+\s+(\w+)",
    re.DOTALL,
)
# @Id field
_ID_FIELD_RE = re.compile(
    r"@Id\b.*?(?:private|protected)\s+\S+\s+(\w+)",
    re.DOTALL,
)
# Constraint annotations stacked before field
_CONSTRAINT_RE = re.compile(
    r"@(NotNull|NotEmpty|Size|Min|Max|Pattern|Email|DecimalMin|DecimalMax)\s*(?:\([^)]*\))?"
)
# @ManyToOne, @OneToMany, @OneToOne, @ManyToMany
_RELATION_RE = re.compile(
    r"@(ManyToOne|OneToMany|OneToOne|ManyToMany)\s*(?:\([^)]*\))?"
    r".*?(?:private|protected)\s+\S+\s+(\w+)",
    re.DOTALL,
)
# nullable=false in @Column
_NULLABLE_FALSE_RE = re.compile(r"nullable\s*=\s*false", re.IGNORECASE)
# length=N in @Column
_LENGTH_RE = re.compile(r"length\s*=\s*(\d+)", re.IGNORECASE)
# unique=true in @Column
_UNIQUE_TRUE_RE = re.compile(r"unique\s*=\s*true", re.IGNORECASE)
# @Version
_VERSION_RE = re.compile(r"@Version\b")
# Enum type
_ENUM_TYPE_RE = re.compile(r"@Enumerated\b")
# Inline field after annotation(s) on same line
_INLINE_FIELD_RE = re.compile(
    r"(?:private|protected|public)\s+(?:(?:static|final|transient)\s+)*\S+\s+(\w+)\s*[;=]"
)
# Plain field declaration (no leading annotation)
_PLAIN_FIELD_RE = re.compile(
    r"^\s*(?:(?:private|protected|public|static|final|transient)\s+)*\S+\s+(\w+)\s*[;=]"
)


def _match_field(line: str, stripped: str) -> re.Match[str] | None:
    """Return a match whose group(1) is the Java field name, or None.

    On annotation-bearing lines use _INLINE_FIELD_RE first so that
    '@Column(nullable=false, length=34) private String iban;' captures
    'iban' rather than the annotation arg 'length'.
    """
    if "@" in stripped:
        return _INLINE_FIELD_RE.search(line)
    return _PLAIN_FIELD_RE.match(line) or _INLINE_FIELD_RE.search(line)


def _col_params(args_str: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([\w.]+))', args_str):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        params[key] = val
    return params


def _jpa_meta_rules(
    entity: str,
    field: str,
    *,
    nullable_false: bool,
    col_length: str | None,
    col_unique: bool,
    version: bool,
) -> list[dict[str, Any]]:
    """Emit JPA-metadata validation rules (@Column attrs + @Version)."""
    rules: list[dict[str, Any]] = []
    base: dict[str, Any] = {
        "entity": entity,
        "field": field,
        "message": "",
        "error_i18n_key": "",
        "cross_field": False,
    }
    if nullable_false:
        rules.append({**base, "constraint": "ColumnNotNull", "params": {}})
    if col_length:
        rules.append({**base, "constraint": "Length", "params": {"max": col_length}})
    if col_unique:
        rules.append({**base, "constraint": "Unique", "params": {}})
    if version:
        rules.append({**base, "constraint": "OptimisticLock", "params": {}})
    return rules


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file is not a JPA @Entity."""
    if Path(file_path).suffix.lower() != ".java":
        return {}
    if not any(sig in content for sig in _ENTITY_SIGNAL):
        return {}

    class_name_m = _CLASS_NAME_RE.search(content)
    entity_name = class_name_m.group(1) if class_name_m else str(Path(file_path).stem)

    form_fields: list[dict[str, Any]] = []
    validation_rules: list[dict[str, Any]] = []
    order = 0

    # Walk line-by-line accumulating annotations then binding to field
    lines = content.split("\n")
    pending_constraints: list[str] = []
    pending_nullable_false = False
    pending_column_length: str | None = None
    pending_column_unique = False
    pending_version = False
    pending_enum = False

    def _reset() -> None:
        nonlocal pending_constraints, pending_nullable_false, pending_column_length
        nonlocal pending_column_unique, pending_version, pending_enum
        pending_constraints = []
        pending_nullable_false = False
        pending_column_length = None
        pending_column_unique = False
        pending_version = False
        pending_enum = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Constraint annotations
        for c in _CONSTRAINT_RE.finditer(stripped):
            pending_constraints.append(c.group(1))
        if _NULLABLE_FALSE_RE.search(stripped):
            pending_nullable_false = True
        lm = _LENGTH_RE.search(stripped)
        if lm:
            pending_column_length = lm.group(1)
        if _UNIQUE_TRUE_RE.search(stripped):
            pending_column_unique = True
        if _VERSION_RE.search(stripped):
            pending_version = True
        if _ENUM_TYPE_RE.search(stripped):
            pending_enum = True

        # Field declaration: ends the annotation block.
        field_m = _match_field(line, stripped)
        has_jpa_meta = (
            pending_nullable_false
            or pending_column_length
            or pending_column_unique
            or pending_version
        )
        if field_m and (pending_constraints or has_jpa_meta):
            field_name = field_m.group(1)
            if field_name not in ("id", "serialVersionUID"):
                required = pending_nullable_false or "NotNull" in pending_constraints
                ftype = "select" if pending_enum else "text"
                if pending_constraints:
                    order += 1
                    form_fields.append(
                        {
                            "endpoint_id": "",
                            "name": field_name,
                            "field_type": ftype,
                            "required": required,
                            "constraints": tuple(pending_constraints),
                            "source": "annotation",
                            "display_order": order,
                        }
                    )
                for constraint in pending_constraints:
                    validation_rules.append(
                        {
                            "entity": entity_name,
                            "field": field_name,
                            "constraint": constraint,
                            "params": {},
                            "message": "",
                            "error_i18n_key": "",
                            "cross_field": False,
                        }
                    )
                validation_rules.extend(
                    _jpa_meta_rules(
                        entity_name,
                        field_name,
                        nullable_false=pending_nullable_false,
                        col_length=pending_column_length,
                        col_unique=pending_column_unique,
                        version=pending_version,
                    )
                )
            _reset()
        elif stripped and not stripped.startswith("@") and not stripped.startswith("//"):
            if not re.match(r"^(?:import|package|public|private|protected|class|\*|/)", stripped):
                _reset()

        i += 1

    result: dict[str, Any] = {}
    if form_fields:
        result["form_fields"] = form_fields
    if validation_rules:
        result["validation_rules"] = validation_rules
    return result
