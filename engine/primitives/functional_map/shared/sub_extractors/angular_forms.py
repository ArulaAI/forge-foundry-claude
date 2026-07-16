from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_TS_SIGNALS = ("fb.group(", "FormBuilder", "FormGroup", "FormControl")
_HTML_SIGNALS = ("[(ngModel)]", "mat-form-field", "p-inputText", "p-input")

# Reactive forms: capture fb.group({ ... }) block
_FB_GROUP_RE = re.compile(r"this\.\w+\s*=\s*this\.\w+\.group\s*\(", re.MULTILINE)

# Per-field key in group: fieldName: [
_FIELD_KEY_RE = re.compile(r"(\w+)\s*:\s*\[")

# Validators.X or Validators.X(args)
_VALIDATOR_RE = re.compile(r"Validators\.(\w+)(?:\(([^)]*)\))?")

# Template-driven: [(ngModel)]="model.fieldName"
_NGMODEL_RE = re.compile(r'\[\(ngModel\)\]="[^."]*\.(\w+)"')

# Adjacent required attr (no value — boolean attr)
_REQUIRED_ATTR_RE = re.compile(r"\brequired\b(?!\s*=)")

# Adjacent pattern="..." attr
_PATTERN_ATTR_RE = re.compile(r'pattern="([^"]+)"')

# Angular Material / PrimeNG field tags
_MATERIAL_TAG_RE = re.compile(r"<(?:mat-form-field|mat-input|p-inputText|p-input\w*)")


def _extract_balanced(text: str, start: int, open_ch: str, close_ch: str) -> str:
    """Return substring from start up to and including the matching close character."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def _parse_validator(
    name: str, args: str, field: str, entity: str
) -> tuple[bool, dict[str, Any] | None]:
    """Map a Validators.X call to (required_flag, validation_rule_dict | None)."""
    name_lower = name.lower()
    if name_lower == "required":
        return True, None
    constraint_map = {
        "min": "Min",
        "max": "Max",
        "minlength": "MinLength",
        "maxlength": "MaxLength",
        "pattern": "Pattern",
    }
    constraint = constraint_map.get(name_lower)
    if constraint is None:
        return False, None
    params_key = {
        "Min": "min",
        "Max": "max",
        "MinLength": "minLength",
        "MaxLength": "maxLength",
        "Pattern": "regexp",
    }[constraint]
    # Strip surrounding quotes from args
    raw_arg = args.strip().strip("'\"") if args else ""
    rule: dict[str, Any] = {
        "entity": entity,
        "field": field,
        "constraint": constraint,
        "params": {params_key: raw_arg},
        "message": "",
        "error_i18n_key": "",
        "cross_field": False,
    }
    return False, rule


def _parse_ts(file_path: Path, content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract form_fields + validation_rules from reactive-form TypeScript."""
    form_fields: list[dict[str, Any]] = []
    validation_rules: list[dict[str, Any]] = []
    entity = Path(file_path).stem

    for m in _FB_GROUP_RE.finditer(content):
        # Find the opening paren of .group(
        paren_pos = content.find("(", m.end() - 1)
        if paren_pos == -1:
            continue
        # Find the opening brace inside the paren
        brace_pos = content.find("{", paren_pos)
        if brace_pos == -1:
            continue
        block = _extract_balanced(content, brace_pos, "{", "}")
        if not block:
            continue

        order = 0
        seen: set[str] = set()
        for km in _FIELD_KEY_RE.finditer(block):
            field_name = km.group(1)
            if field_name in seen:
                continue
            seen.add(field_name)

            # Slice from the field key to the next field key or end of block
            field_start = km.end()
            next_key = _FIELD_KEY_RE.search(block, km.end())
            field_slice = block[field_start : next_key.start() if next_key else len(block)]

            required = False
            constraints: list[str] = []
            for vm in _VALIDATOR_RE.finditer(field_slice):
                v_name = vm.group(1)
                v_args = vm.group(2) or ""
                is_req, rule = _parse_validator(v_name, v_args, field_name, entity)
                if is_req:
                    required = True
                if rule:
                    constraints.append(rule["constraint"])
                    validation_rules.append(rule)

            if required:
                validation_rules.append(
                    {
                        "entity": entity,
                        "field": field_name,
                        "constraint": "NotNull",
                        "params": {},
                        "message": "",
                        "error_i18n_key": "",
                        "cross_field": False,
                    }
                )

            order += 1
            form_fields.append(
                {
                    "endpoint_id": "",
                    "name": field_name,
                    "field_type": "text",
                    "required": required,
                    "constraints": tuple(constraints),
                    "source": "angular",
                    "display_order": order,
                }
            )

    return form_fields, validation_rules


def _parse_html(file_path: Path, content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract form_fields from template-driven HTML and Angular Material/PrimeNG tags."""
    form_fields: list[dict[str, Any]] = []
    validation_rules: list[dict[str, Any]] = []
    entity = Path(file_path).stem

    seen: set[str] = set()
    order = 0

    # Template-driven: [(ngModel)]="..."
    for m in _NGMODEL_RE.finditer(content):
        field_name = m.group(1)
        if field_name in seen:
            continue
        seen.add(field_name)

        # Look at surrounding tag context (from last < to next >)
        tag_start = content.rfind("<", 0, m.start())
        tag_end = content.find(">", m.end())
        tag_ctx = content[tag_start : tag_end + 1] if tag_end != -1 else content[tag_start:]

        required = bool(_REQUIRED_ATTR_RE.search(tag_ctx))
        constraints: list[str] = []
        pat_m = _PATTERN_ATTR_RE.search(tag_ctx)
        if pat_m:
            constraints.append("Pattern")
            validation_rules.append(
                {
                    "entity": entity,
                    "field": field_name,
                    "constraint": "Pattern",
                    "params": {"regexp": pat_m.group(1)},
                    "message": "",
                    "error_i18n_key": "",
                    "cross_field": False,
                }
            )
        if required:
            validation_rules.append(
                {
                    "entity": entity,
                    "field": field_name,
                    "constraint": "NotNull",
                    "params": {},
                    "message": "",
                    "error_i18n_key": "",
                    "cross_field": False,
                }
            )

        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": field_name,
                "field_type": "text",
                "required": required,
                "constraints": tuple(constraints),
                "source": "angular",
                "display_order": order,
            }
        )

    # Angular Material / PrimeNG tags without ngModel (source hint only)
    for m in _MATERIAL_TAG_RE.finditer(content):
        tag_end = content.find(">", m.end())
        tag_ctx = content[m.start() : tag_end + 1] if tag_end != -1 else content[m.start() :]
        # Skip if already captured via ngModel
        ngm = _NGMODEL_RE.search(tag_ctx)
        if ngm:
            field_name = ngm.group(1)
            if field_name not in seen:
                seen.add(field_name)
                order += 1
                form_fields.append(
                    {
                        "endpoint_id": "",
                        "name": field_name,
                        "field_type": "text",
                        "required": False,
                        "constraints": (),
                        "source": "angular",
                        "display_order": order,
                    }
                )

    return form_fields, validation_rules


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file has no Angular form signals."""
    suffix = Path(file_path).suffix.lower()
    if suffix == ".ts":
        if not any(sig in content for sig in _TS_SIGNALS):
            return {}
        form_fields, validation_rules = _parse_ts(file_path, content)
    elif suffix == ".html":
        if not any(sig in content for sig in _HTML_SIGNALS):
            return {}
        form_fields, validation_rules = _parse_html(file_path, content)
    else:
        return {}

    result: dict[str, Any] = {}
    if form_fields:
        result["form_fields"] = form_fields
    if validation_rules:
        result["validation_rules"] = validation_rules
    return result
