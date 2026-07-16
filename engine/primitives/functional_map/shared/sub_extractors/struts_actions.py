from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET


def _parse_struts2(content: str, file_path: Path) -> dict[str, list[Any]]:
    """Parse struts.xml (Struts 2)."""
    endpoints: list[dict[str, Any]] = []
    nav_edges: list[dict[str, Any]] = []
    try:
        clean = re.sub(r"<!DOCTYPE[^>]*>", "", content)
        root = ET.fromstring(clean)
    except ET.ParseError:
        return {}

    for pkg in root.iter("package"):
        namespace = pkg.get("namespace", "")
        for action in pkg.iter("action"):
            name = action.get("name", "")
            cls = action.get("class", "")
            path = (namespace.rstrip("/") + "/" + name).lstrip("/")
            if not path.startswith("/"):
                path = "/" + path
            endpoints.append(
                {
                    "path": path,
                    "method": "POST",
                    "handler": cls or name,
                    "file": str(file_path),
                    "line": 0,
                }
            )
            for result in action.iter("result"):
                result_path = (result.text or "").strip()
                if result_path:
                    nav_edges.append(
                        {
                            "from_screen": path,
                            "to_screen": result_path,
                            "kind": "redirect",
                            "file": str(file_path),
                            "line": 0,
                        }
                    )
    return {"endpoints": endpoints, "nav_edges": nav_edges} if endpoints else {}


def _parse_struts1(content: str, file_path: Path) -> dict[str, list[Any]]:
    """Parse struts-config.xml (Struts 1)."""
    endpoints: list[dict[str, Any]] = []
    nav_edges: list[dict[str, Any]] = []
    try:
        clean = re.sub(r"<!DOCTYPE[^>]*>", "", content)
        root = ET.fromstring(clean)
    except ET.ParseError:
        return {}

    for action in root.iter("action"):
        path = action.get("path", "")
        action_type = action.get("type", "")
        if not path.startswith("/"):
            path = "/" + path
        endpoints.append(
            {
                "path": path,
                "method": "POST",
                "handler": action_type or path,
                "file": str(file_path),
                "line": 0,
            }
        )
        for fwd in action.iter("forward"):
            fwd_path = fwd.get("path", "")
            if fwd_path:
                nav_edges.append(
                    {
                        "from_screen": path,
                        "to_screen": fwd_path,
                        "kind": "redirect",
                        "file": str(file_path),
                        "line": 0,
                    }
                )
    return {"endpoints": endpoints, "nav_edges": nav_edges} if endpoints else {}


def _parse_struts2_validation(content: str, file_path: Path) -> dict[str, list[Any]]:
    """Parse *-validation.xml (Struts 2 field validators)."""
    validation_rules: list[dict[str, Any]] = []
    entity = Path(file_path).stem.replace("-validation", "")
    try:
        clean = re.sub(r"<!DOCTYPE[^>]*>", "", content)
        root = ET.fromstring(clean)
    except ET.ParseError:
        return {}

    for field_el in root.iter("field"):
        field_name = field_el.get("name", "")
        for validator in field_el.iter("field-validator"):
            vtype = validator.get("type", "")
            constraint = {
                "requiredstring": "NotNull",
                "required": "NotNull",
                "int": "Min",
                "double": "Min",
                "date": "DateFormat",
                "email": "Email",
                "url": "URL",
                "regex": "Pattern",
                "stringlength": "Size",
                "fieldexpression": "AssertTrue",
            }.get(vtype, vtype)

            params: dict[str, str] = {}
            message = ""
            for param in validator.iter("param"):
                params[param.get("name", "")] = (param.text or "").strip()
            for msg in validator.iter("message"):
                message = (msg.text or "").strip()

            if "expression" in params:
                params["regexp"] = params.pop("expression")

            validation_rules.append(
                {
                    "entity": entity,
                    "field": field_name,
                    "constraint": constraint,
                    "params": params,
                    "message": message,
                    "error_i18n_key": "",
                    "cross_field": False,
                }
            )

    return {"validation_rules": validation_rules} if validation_rules else {}


def parse(file_path: Path, content: str) -> dict[str, list[Any]]:
    """Parses struts-config.xml, struts.xml, and *-validation.xml."""
    name = Path(file_path).name
    if name == "struts.xml" or (name != "struts-config.xml" and "<struts>" in content):
        return _parse_struts2(content, file_path)
    if name == "struts-config.xml" or "<struts-config" in content:
        return _parse_struts1(content, file_path)
    if name.endswith("-validation.xml") or "<validators>" in content:
        return _parse_struts2_validation(content, file_path)
    return {}
