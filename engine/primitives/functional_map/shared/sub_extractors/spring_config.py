from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Properties key=value
_PROP_RE = re.compile(r"^([^#!\s][^=:]*?)\s*[=:]\s*(.*)$")

# Feature-flag key heuristic: prefix "feature." OR suffix ".enabled"/".disabled"
_FEATURE_KEY_RE = re.compile(r"^feature\..+|.+\.(enabled|disabled)$")

# @ConditionalOnProperty: name="..." or "..." positional
_CONDITIONAL_RE = re.compile(r'@ConditionalOnProperty\s*\(\s*(?:name\s*=\s*)?["\']([^"\']+)["\']')

# @Value("${feature.X...}") field injection; capture the property key
_VALUE_RE = re.compile(r'@Value\s*\(\s*"\$\{([^:}]+)')

# Field name: variable declared after @Value annotation
_FIELD_DECL_RE = re.compile(r"\bprivate\b[^;]*?\b(\w+)\s*;")
_LOCAL_VAR_RE = re.compile(r"\b(?:boolean|Boolean|String|int|Integer)\s+(\w+)")

# @Controller / @RestController gate (M1)
_CONTROLLER_RE = re.compile(r"@(?:Rest)?Controller\b")

# Handler class name
_CLASS_NAME_RE = re.compile(r"\bclass\s+(\w+)")

# Bare "if (" anywhere in a line
_IF_RE = re.compile(r"\bif\s*\(")


def _is_feature_flag(key: str) -> bool:
    return bool(_FEATURE_KEY_RE.match(key))


def _collect_app_flags(content: str, suffix: str) -> list[str]:
    """Parse application*.properties or application*.yml for feature-flag keys (internal use)."""
    if suffix == ".properties":
        flags: list[str] = []
        for raw_line in content.splitlines():
            stripped = raw_line.rstrip()
            if not stripped or stripped.startswith("#") or stripped.startswith("!"):
                continue
            m = _PROP_RE.match(stripped)
            if m and _is_feature_flag(m.group(1).strip()):
                flags.append(m.group(1).strip())
        return flags
    flags_yaml: list[str] = []
    stack: list[tuple[int, str]] = []
    for line in content.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([a-zA-Z0-9_.\-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        indent = len(m.group(1))
        key_part = m.group(2)
        value = m.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        prefix = ".".join(s[1] for s in stack) + ("." if stack else "") + key_part
        if value and not value.endswith(":"):
            if _is_feature_flag(prefix):
                flags_yaml.append(prefix)
        else:
            stack.append((indent, key_part))
    return flags_yaml


def _parse_java_conditional(content: str) -> list[dict[str, Any]]:
    # M1: gate to @Controller / @RestController classes only
    if not _CONTROLLER_RE.search(content):
        return []

    bcs: list[dict[str, Any]] = []
    lines = content.splitlines()

    # C2: class name for handler matching in extractor bc_by_handler
    class_name = ""
    for line in lines:
        cm = _CLASS_NAME_RE.search(line)
        if cm:
            class_name = cm.group(1)
            break

    # @ConditionalOnProperty — hard flag
    for _i, line in enumerate(lines):
        m = _CONDITIONAL_RE.search(line)
        if not m:
            continue
        flag_key = m.group(1)
        bcs.append(
            {
                "endpoint_id": "",
                "handler": class_name,
                "conditional": True,
                "conditional_flag": f"hard:{flag_key}",
                "source": "annotation",
            }
        )

    # @Value("${feature...}") — soft flag (H1 fix: match field name, not last segment)
    for i, line in enumerate(lines):
        m = _VALUE_RE.search(line)
        if not m:
            continue
        flag_key = m.group(1)
        if not _is_feature_flag(flag_key):
            continue
        # H1: capture the injected field name from the variable declaration
        field_name = ""
        for j in range(i + 1, min(i + 5, len(lines))):
            fd = _FIELD_DECL_RE.search(lines[j])
            if fd:
                field_name = fd.group(1)
                break
            lv = _LOCAL_VAR_RE.search(lines[j])
            if lv:
                field_name = lv.group(1)
                break
        # Match field name (primary) or any dotted segment (fallback) against if(...)
        window = lines[i + 1 : i + 41]
        candidates: set[str] = set(flag_key.split("."))
        if field_name:
            candidates.add(field_name)
        has_if = any(_IF_RE.search(wl) and any(c and c in wl for c in candidates) for wl in window)
        if not has_if:
            continue
        bcs.append(
            {
                "endpoint_id": "",
                "handler": class_name,
                "conditional": True,
                "conditional_flag": f"soft:{flag_key}",
                "source": "annotation",
            }
        )

    return bcs


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file is not a Spring conditional Java file.
    application*.properties/yml are parsed but produce no output (C1: no digest field
    for feature_flags; value realized only via @ConditionalOnProperty tagging on controllers).
    """
    name = Path(file_path).name.lower()
    suffix = Path(file_path).suffix.lower()

    is_app_config = name.startswith("application") and suffix in (".properties", ".yml", ".yaml")
    is_java_conditional = suffix == ".java" and (
        "@ConditionalOnProperty" in content or '@Value("${' in content
    )

    if not is_app_config and not is_java_conditional:
        return {}

    # C1: application config parsed for flag inventory only — no standalone output
    if is_app_config:
        _collect_app_flags(content, suffix)
        return {}

    result: dict[str, Any] = {}
    if is_java_conditional:
        bcs = _parse_java_conditional(content)
        if bcs:
            result["business_contexts"] = bcs

    return result
