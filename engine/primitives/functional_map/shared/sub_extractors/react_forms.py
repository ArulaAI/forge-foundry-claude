from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_FORM_SIGNALS = (
    "useForm",
    "react-hook-form",
    "formik",
    "Formik",
    "<form",
    "handleSubmit",
    "register(",
)

# react-hook-form register: register('fieldName', ...) — field name only;
# options are extracted via brace-balanced scan to handle nested braces.
_RHF_REGISTER_NAME_RE = re.compile(r"register\s*\(\s*['\"]([^'\"]+)['\"]")
# Formik: name="fieldName"
_FORMIK_NAME_RE = re.compile(r'name=["\']([^"\']+)["\']')
# Controlled input with value state: value={X} onChange
_CONTROLLED_VALUE_RE = re.compile(r"(?:value|defaultValue)\s*=\s*\{(\w+)\}[^>]*onChange")
# required constraint detection in register options
_REQUIRED_RE = re.compile(r"required\s*:", re.IGNORECASE)
_PATTERN_RE = re.compile(r"pattern\s*:\s*\{.{0,120}value\s*:\s*/([^/\n]+)/", re.DOTALL)
_MIN_RE = re.compile(r"\bmin\s*:\s*\{.{0,80}value\s*:\s*([\d.]+)", re.DOTALL)
_MAX_RE = re.compile(r"\bmax\s*:\s*\{.{0,80}value\s*:\s*([\d.]+)", re.DOTALL)
_MAXLENGTH_RE = re.compile(r"maxLength\s*:\s*\{.{0,80}value\s*:\s*(\d+)", re.DOTALL)

# Input type detection
_TYPE_RE = re.compile(r'type=["\']([^"\']+)["\']')

# Select with register
_SELECT_REGISTER_RE = re.compile(r'<select[^>]*\{\.\.\.register\s*\(\s*["\']([^"\']+)["\']')
# Textarea with register
_TEXTAREA_REGISTER_RE = re.compile(r'<textarea[^>]*\{\.\.\.register\s*\(\s*["\']([^"\']+)["\']')

# API endpoint from axios.post in onSubmit
_API_ENDPOINT_RE = re.compile(
    r'(?:axios|fetch)\s*\.\s*(post|put|patch|get|delete)\s*\(\s*["\']([^"\']+)["\']'
)


def _extract_balanced_braces(text: str, start: int) -> str:
    """Return the substring from `start` up to and including the matching `}`.

    `start` must point at the opening `{`. Returns empty string if not found.
    """
    if start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def _extract_constraints(options_str: str) -> tuple[bool, dict[str, str]]:
    """Extract required + validation constraints from RHF register options."""
    if not options_str:
        return False, {}
    required = bool(_REQUIRED_RE.search(options_str))
    params: dict[str, str] = {}
    m = _PATTERN_RE.search(options_str)
    if m:
        params["regexp"] = m.group(1)
    m = _MIN_RE.search(options_str)
    if m:
        params["min"] = m.group(1)
    m = _MAX_RE.search(options_str)
    if m:
        params["max"] = m.group(1)
    m = _MAXLENGTH_RE.search(options_str)
    if m:
        params["maxLength"] = m.group(1)
    return required, params


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file has no React form signals."""
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".tsx", ".jsx", ".ts", ".js"):
        return {}
    if not any(sig in content for sig in _FORM_SIGNALS):
        return {}
    if "<form" not in content and "useForm" not in content and "Formik" not in content:
        return {}

    form_fields: list[dict[str, Any]] = []
    validation_rules: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []
    order = 0

    component_name = str(Path(file_path).stem)

    # Select fields (detect before generic register sweep)
    select_fields: set[str] = {m.group(1) for m in _SELECT_REGISTER_RE.finditer(content)}
    textarea_fields: set[str] = {m.group(1) for m in _TEXTAREA_REGISTER_RE.finditer(content)}

    # react-hook-form register calls
    seen: set[str] = set()
    for m in _RHF_REGISTER_NAME_RE.finditer(content):
        field_name = m.group(1)
        # Extract options object using brace-balanced scan (handles nested braces)
        after_name = content[m.end() :]
        comma_match = re.match(r"\s*,\s*", after_name)
        if comma_match:
            rest = after_name[comma_match.end() :]
            brace_pos = rest.find("{")
            options_str = _extract_balanced_braces(rest, brace_pos) if brace_pos != -1 else ""
        else:
            options_str = ""
        if field_name in seen:
            continue
        seen.add(field_name)

        required, params = _extract_constraints(options_str)

        # Determine field type from context
        pos = m.start()
        tag_start = content.rfind("<", 0, pos)
        tag_end = content.find(">", pos)
        tag_ctx = content[tag_start : tag_end + 1] if tag_end > -1 else ""
        type_m = _TYPE_RE.search(tag_ctx)
        if field_name in select_fields or "<select" in tag_ctx:
            ftype = "select"
        elif field_name in textarea_fields or "<textarea" in tag_ctx:
            ftype = "textarea"
        elif type_m:
            ftype = type_m.group(1)
        else:
            ftype = "text"

        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": field_name,
                "field_type": ftype,
                "required": required,
                "constraints": tuple(params.keys()),
                "source": "annotation",
                "display_order": order,
            }
        )

        if params or required:
            if required:
                validation_rules.append(
                    {
                        "entity": component_name,
                        "field": field_name,
                        "constraint": "NotNull",
                        "params": {},
                        "message": "",
                        "error_i18n_key": "",
                        "cross_field": False,
                    }
                )
            for k, v in params.items():
                constraint = {
                    "regexp": "Pattern",
                    "min": "Min",
                    "max": "Max",
                    "maxLength": "Size",
                }.get(k, k)
                validation_rules.append(
                    {
                        "entity": component_name,
                        "field": field_name,
                        "constraint": constraint,
                        "params": {k: v},
                        "message": "",
                        "error_i18n_key": "",
                        "cross_field": False,
                    }
                )

    # API endpoints from axios/fetch calls in submit handlers
    for m in _API_ENDPOINT_RE.finditer(content):
        method = m.group(1).upper()
        path = m.group(2)
        endpoints.append(
            {
                "path": path,
                "method": method,
                "handler": component_name,
                "file": str(file_path),
                "line": content[: m.start()].count("\n") + 1,
            }
        )

    result: dict[str, Any] = {}
    if form_fields:
        result["form_fields"] = form_fields
    if validation_rules:
        result["validation_rules"] = validation_rules
    if endpoints:
        result["endpoints"] = endpoints
    return result
