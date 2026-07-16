from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "{" in tag else tag


def parse(file_path: Path, content: str) -> dict[str, list[dict[str, Any]]]:
    """Parse Spring Integration XML and @MessagingGateway Java files."""
    suffix = Path(file_path).suffix.lower()
    name = Path(file_path).name

    if suffix == ".xml":
        is_integration = (
            "spring-integration" in content
            or "<int:" in content
            or name.endswith("-integration.xml")
            or ("<route>" in content and "camel" in name.lower())
        )
        if not is_integration:
            return {}

        endpoints: list[dict[str, Any]] = []
        try:
            clean = re.sub(r"<!DOCTYPE[^>]*>", "", content)
            root = ET.fromstring(clean)
        except ET.ParseError:
            return {}

        for el in root.iter():
            tag = _strip_ns(el.tag)
            if tag == "gateway":
                iface = el.get("service-interface", el.get("id", "gateway"))
                endpoints.append(
                    {
                        "path": f"/integration/{iface}",
                        "method": "SYSTEM",
                        "handler": iface,
                        "file": str(file_path),
                        "line": 0,
                    }
                )
            if tag == "from":
                uri = el.get("uri", "")
                if uri:
                    endpoints.append(
                        {
                            "path": f"/camel/{uri.replace(':', '/')}",
                            "method": "SYSTEM",
                            "handler": uri,
                            "file": str(file_path),
                            "line": 0,
                        }
                    )

        return {"endpoints": endpoints} if endpoints else {}

    if suffix == ".java":
        if "@MessagingGateway" not in content and "@Gateway" not in content:
            return {}
        endpoints = []
        cls_m = re.search(r"@MessagingGateway[^)]*\)?\s*.*?interface\s+(\w+)", content, re.DOTALL)
        iface = cls_m.group(1) if cls_m else "Gateway"
        for m in re.finditer(
            r"@Gateway[^)]*\)?\s*.*?(?:public)?\s+\S+\s+(\w+)\s*\(",
            content,
            re.DOTALL,
        ):
            method_name = m.group(1)
            endpoints.append(
                {
                    "path": f"/integration/{iface}/{method_name}",
                    "method": "SYSTEM",
                    "handler": f"{iface}.{method_name}",
                    "file": str(file_path),
                    "line": 0,
                }
            )
        return {"endpoints": endpoints} if endpoints else {}

    return {}
