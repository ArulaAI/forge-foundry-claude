from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Properties file: key=value or key: value
_PROP_RE = re.compile(r"^([^#!\s][^=:]*?)\s*[=:]\s*(.*)$")
# Locale from filename: messages_es.properties → "es", messages_es_MX → "es_MX"
_LOCALE_RE = re.compile(r"messages_([a-z]{2}(?:_[A-Z]{2})?)(?:\.properties)?$", re.IGNORECASE)

# JSON i18n flat key/value
# YAML (best-effort flat key extraction)
_YAML_KEY_RE = re.compile(r"^(\s*)([a-zA-Z0-9_.\-]+)\s*:\s*(.+)$")


def _detect_locale(file_path: Path) -> str:
    m = _LOCALE_RE.search(file_path.name)
    return m.group(1) if m else "default"


def _parse_properties(content: str, locale: str, _file_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in content.splitlines():
        stripped_line = line.rstrip()
        if not stripped_line or stripped_line.startswith("#") or stripped_line.startswith("!"):
            continue
        m = _PROP_RE.match(stripped_line)
        if m:
            key = m.group(1).strip()
            value = m.group(2).strip()
            # Handle line continuation
            while value.endswith("\\"):
                value = value[:-1]
            entries.append({"key": key, "value": value, "locale": locale})
    return entries


def _parse_json(content: str, locale: str, _file_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return entries

    def _flatten(obj: object, prefix: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                _flatten(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(obj, str):
            entries.append({"key": prefix, "value": obj, "locale": locale})

    _flatten(data, "")
    return entries


def _parse_yaml_flat(content: str, locale: str, _file_path: Path) -> list[dict[str, Any]]:
    """Best-effort flat YAML key extraction (no PyYAML dependency)."""
    entries: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []  # (indent_level, key_prefix)
    for line in content.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = _YAML_KEY_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        key_part = m.group(2)
        value = m.group(3).strip().strip("\"'")

        # Pop stack to current indent level
        while stack and stack[-1][0] >= indent:
            stack.pop()

        prefix = ".".join(s[1] for s in stack) + ("." if stack else "") + key_part

        if value and not value.endswith(":"):
            entries.append({"key": prefix, "value": value, "locale": locale})
        else:
            stack.append((indent, key_part))
    return entries


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file is not a recognized i18n resource file."""
    suffix = Path(file_path).suffix.lower()
    name = Path(file_path).name.lower()
    locale = _detect_locale(file_path)

    entries: list[dict[str, Any]] = []

    if suffix == ".properties":
        if not any(sig in name for sig in ("message", "label", "error", "i18n", "bundle")):
            return {}
        entries = _parse_properties(content, locale, file_path)

    elif suffix == ".json":
        # Only pick up i18n-shaped JSON (flat or nested string values)
        if not any(sig in name for sig in ("message", "i18n", "locale", "translation", "lang")):
            return {}
        entries = _parse_json(content, locale, file_path)

    elif suffix in (".yaml", ".yml"):
        if not any(sig in name for sig in ("message", "i18n", "locale", "translation")):
            return {}
        entries = _parse_yaml_flat(content, locale, file_path)

    else:
        return {}

    if not entries:
        return {}
    return {"i18n_entries": entries}
