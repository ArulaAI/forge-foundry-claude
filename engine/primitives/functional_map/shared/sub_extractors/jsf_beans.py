from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET

_ROLES_ALLOWED_RE = re.compile(r"@RolesAllowed\s*\(\s*\{([^}]+)\}\s*\)")
_JSR380_RE = re.compile(
    r"@(NotNull|NotEmpty|Size|Min|Max|Pattern|Email)\s*(?:\([^)]*\))?\s*"
    r"(?:private|protected)\s+\S+\s+(\w+)"
)
_CLASS_NAME_RE = re.compile(r"class\s+(\w+)")
_MANAGED_BEAN_SIGNALS = ("@ManagedBean", "@Named")


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "{" in tag else tag


def _parse_faces_config(content: str, file_path: Path) -> dict[str, Any]:
    nav_edges: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}

    for nav_rule in root.iter():
        if _strip_ns(nav_rule.tag) == "navigation-rule":
            from_view = ""
            for child in nav_rule:
                if _strip_ns(child.tag) == "from-view-id":
                    from_view = (child.text or "").strip()
                if _strip_ns(child.tag) == "navigation-case":
                    to_view = ""
                    for case_child in child:
                        if _strip_ns(case_child.tag) == "to-view-id":
                            to_view = (case_child.text or "").strip()
                    if from_view and to_view:
                        nav_edges.append(
                            {
                                "from_screen": from_view,
                                "to_screen": to_view,
                                "kind": "link",
                                "file": str(file_path),
                                "line": 0,
                            }
                        )
    return {"nav_edges": nav_edges} if nav_edges else {}


def _parse_xhtml(content: str, _file_path: Path) -> dict[str, Any]:
    form_fields: list[dict[str, Any]] = []
    order = 0

    for m in re.finditer(r"<h:inputText\s([^>]*?)/?>", content, re.DOTALL):
        attrs = m.group(1)
        val_m = re.search(r'value="\#\{[^.]+\.([^}]+)\}"', attrs)
        id_m = re.search(r'id="([^"]+)"', attrs)
        name = (val_m.group(1) if val_m else None) or (id_m.group(1) if id_m else None)
        if not name:
            continue
        required = bool(re.search(r'required="true"', attrs, re.IGNORECASE))
        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": name,
                "field_type": "text",
                "required": required,
                "constraints": (),
                "source": "jsp",
                "display_order": order,
            }
        )

    for m in re.finditer(r"<h:selectOneMenu\s([^>]*?)>", content, re.DOTALL):
        attrs = m.group(1)
        val_m = re.search(r'value="\#\{[^.]+\.([^}]+)\}"', attrs)
        id_m = re.search(r'id="([^"]+)"', attrs)
        name = (val_m.group(1) if val_m else None) or (id_m.group(1) if id_m else None)
        if name:
            order += 1
            form_fields.append(
                {
                    "endpoint_id": "",
                    "name": name,
                    "field_type": "select",
                    "required": False,
                    "constraints": (),
                    "source": "jsp",
                    "display_order": order,
                }
            )

    for m in re.finditer(r"<h:inputSecret\s([^>]*?)/?>", content, re.DOTALL):
        attrs = m.group(1)
        val_m = re.search(r'value="\#\{[^.]+\.([^}]+)\}"', attrs)
        id_m = re.search(r'id="([^"]+)"', attrs)
        name = (val_m.group(1) if val_m else None) or (id_m.group(1) if id_m else None)
        if name:
            order += 1
            form_fields.append(
                {
                    "endpoint_id": "",
                    "name": name,
                    "field_type": "password",
                    "required": False,
                    "constraints": (),
                    "source": "jsp",
                    "display_order": order,
                }
            )

    return {"form_fields": form_fields} if form_fields else {}


def parse(file_path: Path, content: str) -> dict[str, Any]:
    suffix = Path(file_path).suffix.lower()
    name = Path(file_path).name

    if name == "faces-config.xml" or "faces-config" in name:
        return _parse_faces_config(content, file_path)

    if suffix == ".xhtml":
        if "<h:form" not in content and "<h:inputText" not in content:
            return {}
        return _parse_xhtml(content, file_path)

    if suffix == ".java":
        if not any(sig in content for sig in _MANAGED_BEAN_SIGNALS):
            return {}
        entity_m = _CLASS_NAME_RE.search(content)
        entity = entity_m.group(1) if entity_m else str(Path(file_path).stem)
        rules: list[dict[str, Any]] = []
        for m in _JSR380_RE.finditer(content):
            rules.append(
                {
                    "entity": entity,
                    "field": m.group(2),
                    "constraint": m.group(1),
                    "params": {},
                    "message": "",
                    "error_i18n_key": "",
                    "cross_field": False,
                }
            )
        guards: list[dict[str, Any]] = []
        for m in _ROLES_ALLOWED_RE.finditer(content):
            roles = [r.strip().strip("\"'") for r in m.group(1).split(",")]
            guards.append(
                {
                    "endpoint_id": "",
                    "kind": "role_required",
                    "roles": roles,
                    "file": str(file_path),
                    "line": 0,
                }
            )
        result: dict[str, Any] = {}
        if rules:
            result["validation_rules"] = rules
        if guards:
            result["guards"] = guards
        return result

    return {}
