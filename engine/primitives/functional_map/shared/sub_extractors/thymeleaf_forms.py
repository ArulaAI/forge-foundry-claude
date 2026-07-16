from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# th:field="*{fieldName}" — the primary form field marker
_TH_FIELD_RE = re.compile(r'th:field="\*\{([^}]+)\}"', re.IGNORECASE)
# th:name="${X}" for hidden inputs
_TH_NAME_RE = re.compile(r'th:name="\$\{([^}]+)\}"', re.IGNORECASE)
# th:errors="*{field}" → server-side validated field
_TH_ERRORS_RE = re.compile(r'th:errors="\*\{([^}]+)\}"', re.IGNORECASE)
# th:action on the form
_TH_ACTION_RE = re.compile(r'th:action="@\{([^}]+)\}"', re.IGNORECASE)
# plain action attribute fallback
_HTML_ACTION_RE = re.compile(r'<form\b[^>]*\baction="([^"]+)"', re.IGNORECASE)
# sec:authorize for role-conditional content
_SEC_AUTHORIZE_RE = re.compile(r"sec:authorize=['\"]hasRole\('([^']+)'\)['\"]", re.IGNORECASE)
# Detect select: <select th:field="*{X}">
_TH_SELECT_RE = re.compile(r'<select\s[^>]*th:field="\*\{([^}]+)\}"', re.IGNORECASE)
# i18n keys from th:text="#{key}"
_TH_MSG_RE = re.compile(r'th:text="[#$]\{([^}]+)\}"', re.IGNORECASE)
# Hidden type
_TYPE_HIDDEN_RE = re.compile(r'type="hidden"', re.IGNORECASE)
# th:if error conditional
_TH_IF_ERROR_RE = re.compile(r"th:if=\"\$\{#fields\.hasErrors\('([^']+)'\)", re.IGNORECASE)


def parse(  # noqa: PLR0912 — legitimately branchy extractor
    file_path: Path, content: str
) -> dict[str, list[Any]]:
    """Returns {} if file has no Thymeleaf markers."""
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".html", ".htm", ".xhtml"):
        return {}
    if "th:" not in content and "thymeleaf" not in content.lower():
        return {}

    form_fields: list[dict[str, Any]] = []
    i18n_refs: list[str] = []
    nav_edges: list[dict[str, Any]] = []
    guards: list[dict[str, Any]] = []
    order = 0

    th_action_m = _TH_ACTION_RE.search(content)
    html_action_m = _HTML_ACTION_RE.search(content)
    form_action = (
        th_action_m.group(1) if th_action_m else (html_action_m.group(1) if html_action_m else "")
    )

    # i18n keys
    for m in _TH_MSG_RE.finditer(content):
        key = m.group(1)
        if "." in key and not key.startswith("spring."):
            i18n_refs.append(key)

    # Fields with server-side validation
    validated_fields: set[str] = {m.group(1) for m in _TH_ERRORS_RE.finditer(content)}

    # Select fields (detect before generic th:field so we can mark them)
    select_fields: set[str] = {m.group(1) for m in _TH_SELECT_RE.finditer(content)}

    # Conditional blocks: track role-gated field names
    conditional_fields: dict[str, str] = {}
    lines = content.split("\n")
    current_role: str | None = None
    depth = 0
    for line in lines:
        sec_m = _SEC_AUTHORIZE_RE.search(line)
        if sec_m:
            current_role = sec_m.group(1).lstrip("ROLE_")
            depth += 1
        if current_role:
            for fm in _TH_FIELD_RE.finditer(line):
                conditional_fields[fm.group(1)] = current_role
        if ("</div>" in line or "</section>" in line) and depth > 0:
            depth -= 1
            if depth == 0:
                current_role = None

    # th:field fields
    for m in _TH_FIELD_RE.finditer(content):
        name = m.group(1)
        # Determine field type
        # Find the tag context around this attribute
        tag_start = content.rfind("<", 0, m.start())
        tag_end = content.find(">", m.start())
        tag_context = content[tag_start : tag_end + 1] if tag_end > -1 else ""
        if name in select_fields or tag_context.startswith("<select"):
            ftype = "select"
        elif _TYPE_HIDDEN_RE.search(tag_context):
            ftype = "hidden"
        else:
            ftype = "text"

        role = conditional_fields.get(name, "")
        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": name,
                "field_type": ftype,
                "required": name in validated_fields,
                "constraints": (),
                "source": "thymeleaf",
                "display_order": order,
                "conditional_role": role,
                "form_action": form_action,
            }
        )

    # th:name hidden fields not captured by th:field
    seen = {f["name"] for f in form_fields}
    for m in _TH_NAME_RE.finditer(content):
        name = m.group(1)
        # Only pick up csrf-style hidden fields here
        if "csrf" not in name.lower():
            continue
        if name in seen:
            continue
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

    # Form action → nav edge
    for m in _TH_ACTION_RE.finditer(content):
        path = m.group(1)
        if path:
            nav_edges.append(
                {
                    "from_screen": str(file_path.stem),
                    "to_screen": path,
                    "kind": "ajax",
                    "file": str(file_path),
                    "line": 0,
                }
            )

    # Security guards from sec:authorize
    for m in _SEC_AUTHORIZE_RE.finditer(content):
        role = m.group(1).lstrip("ROLE_")
        guards.append(
            {
                "endpoint_id": "",
                "kind": "role_required",
                "roles": [role],
                "file": str(file_path),
                "line": 0,
            }
        )

    result: dict[str, list[Any]] = {}
    if form_fields:
        result["form_fields"] = form_fields
    if i18n_refs:
        result["i18n_refs"] = i18n_refs
    if nav_edges:
        result["nav_edges"] = nav_edges
    if guards:
        result["guards"] = guards
    return result
