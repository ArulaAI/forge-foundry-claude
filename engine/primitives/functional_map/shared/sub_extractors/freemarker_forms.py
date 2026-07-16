from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Spring FreeMarker bind macro: <@spring.bind "model.field"/>
_SPRING_BIND_RE = re.compile(r'<@spring\.bind\s+"[^."]+\.([^"]+)"', re.IGNORECASE)
# Spring message macro: <@spring.message "key"/>
_SPRING_MSG_RE = re.compile(r'<@spring\.message\s+"([^"]+)"', re.IGNORECASE)
# FTL message key: ${msg("key")} or ${i18n.get("key")}
_FTL_MSG_RE = re.compile(r'\$\{(?:msg|i18n|bundle)\s*\(\s*"([^"]+)"\s*\)\}', re.IGNORECASE)
# Hidden CSRF: name="_csrf"
_HIDDEN_CSRF_RE = re.compile(r'name\s*=\s*"(_csrf[^"]*)"', re.IGNORECASE)
# <select name="X"> or <select th:field="*{X}">
_SELECT_NAME_RE = re.compile(r'<select\s[^>]*name\s*=\s*"([^"]+)"', re.IGNORECASE)
# <input name="X"> after spring.bind (best effort)
_INPUT_NAME_RE = re.compile(
    r'<input\s[^>]*name\s*=\s*"\$\{spring\.status\.expression\}"', re.IGNORECASE
)
# Form action
_FORM_ACTION_RE = re.compile(r'<form\s[^>]*action\s*=\s*"([^"]+)"', re.IGNORECASE)


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file is not a FreeMarker template or has no form content."""
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".ftl", ".ftlh", ".fm"):
        return {}
    if "<form" not in content and "@spring.bind" not in content:
        return {}

    form_fields: list[dict[str, Any]] = []
    i18n_refs: list[str] = []
    nav_edges: list[dict[str, Any]] = []
    order = 0

    action_m = _FORM_ACTION_RE.search(content)
    form_action = action_m.group(1) if action_m else ""

    # Collect bound fields from @spring.bind macros
    for m in _SPRING_BIND_RE.finditer(content):
        field_name = m.group(1)
        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": field_name,
                "field_type": "text",
                "required": False,
                "constraints": (),
                "source": "freemarker",
                "display_order": order,
                "form_action": form_action,
            }
        )

    # Select fields
    seen = {f["name"] for f in form_fields}
    for m in _SELECT_NAME_RE.finditer(content):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            order += 1
            form_fields.append(
                {
                    "endpoint_id": "",
                    "name": name,
                    "field_type": "select",
                    "required": False,
                    "constraints": (),
                    "source": "freemarker",
                    "display_order": order,
                    "form_action": form_action,
                }
            )

    # Hidden CSRF fields
    for m in _HIDDEN_CSRF_RE.finditer(content):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            order += 1
            form_fields.append(
                {
                    "endpoint_id": "",
                    "name": name,
                    "field_type": "hidden",
                    "required": False,
                    "constraints": (),
                    "source": "hidden",
                    "display_order": order,
                    "form_action": form_action,
                }
            )

    # i18n keys
    for m in _SPRING_MSG_RE.finditer(content):
        i18n_refs.append(m.group(1))
    for m in _FTL_MSG_RE.finditer(content):
        i18n_refs.append(m.group(1))

    # Form action → nav edge
    for m in _FORM_ACTION_RE.finditer(content):
        nav_edges.append(
            {
                "from_screen": str(Path(file_path).stem),
                "to_screen": m.group(1),
                "kind": "ajax",
                "file": str(file_path),
                "line": 0,
            }
        )

    result: dict[str, Any] = {}
    if form_fields:
        result["form_fields"] = form_fields
    if i18n_refs:
        result["i18n_refs"] = i18n_refs
    if nav_edges:
        result["nav_edges"] = nav_edges
    return result
