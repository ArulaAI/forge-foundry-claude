from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "{" in tag else tag


def _parse_wsdl(content: str, file_path: Path) -> dict[str, Any]:
    endpoints: list[dict[str, Any]] = []
    form_fields: list[dict[str, Any]] = []

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}

    # Collect message parts
    messages: dict[str, list[dict[str, Any]]] = {}
    for msg in root.iter():
        if _strip_ns(msg.tag) == "message":
            msg_name = msg.get("name", "")
            parts: list[dict[str, Any]] = []
            for part in msg:
                if _strip_ns(part.tag) == "part":
                    parts.append(
                        {
                            "name": part.get("name", ""),
                            "type": part.get("type", ""),
                        }
                    )
            if msg_name:
                messages[msg_name] = parts

    # portType operations
    for port_type in root.iter():
        if _strip_ns(port_type.tag) == "portType":
            for op in port_type:
                if _strip_ns(op.tag) == "operation":
                    op_name = op.get("name", "")
                    if not op_name:
                        continue
                    endpoints.append(
                        {
                            "path": f"/soap/{op_name}",
                            "method": "RPC",
                            "handler": op_name,
                            "file": str(file_path),
                            "line": 0,
                        }
                    )
                    for inp in op:
                        if _strip_ns(inp.tag) == "input":
                            msg_ref = inp.get("message", "").split(":")[-1]
                            for msg_part in messages.get(msg_ref, []):
                                if msg_part["name"]:
                                    form_fields.append(
                                        {
                                            "endpoint_id": "",
                                            "name": msg_part["name"],
                                            "field_type": "text",
                                            "required": False,
                                            "constraints": (),
                                            "source": "annotation",
                                        }
                                    )

    result: dict[str, Any] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if form_fields:
        result["form_fields"] = form_fields
    return result


def parse(file_path: Path, content: str) -> dict[str, Any]:
    suffix = Path(file_path).suffix.lower()

    if suffix == ".wsdl":
        if "<definitions" not in content and "<portType" not in content:
            return {}
        return _parse_wsdl(content, file_path)

    if suffix == ".java":
        if "@WebService" not in content and "@WebMethod" not in content:
            return {}
        endpoints: list[dict[str, Any]] = []
        cls_m = re.search(r"@WebService[^)]*\)?\s*.*?class\s+(\w+)", content, re.DOTALL)
        service_name = cls_m.group(1) if cls_m else "WebService"
        for m in re.finditer(
            r"@WebMethod[^)]*\)?\s*.*?(?:public|protected)\s+\S+\s+(\w+)\s*\(",
            content,
            re.DOTALL,
        ):
            op_name = m.group(1)
            endpoints.append(
                {
                    "path": f"/soap/{service_name}/{op_name}",
                    "method": "RPC",
                    "handler": f"{service_name}.{op_name}",
                    "file": str(file_path),
                    "line": 0,
                }
            )
        return {"endpoints": endpoints} if endpoints else {}

    return {}
