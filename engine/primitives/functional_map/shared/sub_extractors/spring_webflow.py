from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from defusedxml import ElementTree as ET

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element


def _tag(el: Element) -> str:
    tag = el.tag
    if tag.startswith("{"):
        tag = tag.split("}", 1)[1]
    return tag


def parse(file_path: Path, content: str) -> dict[str, list[Any]]:
    """Returns {} if file is not a Spring WebFlow XML file."""
    name = Path(file_path).name
    if not (
        name.endswith("-flow.xml") or "webflow" in str(file_path).lower() or "<flow" in content
    ):
        return {}
    if "<flow" not in content:
        return {}

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}

    if _tag(root) != "flow":
        return {}

    flow_id = name.replace("-flow.xml", "").replace(".xml", "")
    view_states: list[str] = []
    transitions: list[tuple[str, str, str]] = []
    guards: list[dict[str, Any]] = []

    for child in root:
        tag = _tag(child)
        if tag in ("view-state", "action-state", "end-state"):
            state_id = child.get("id", "")
            if state_id:
                view_states.append(state_id)

            for subchild in child:
                subtag = _tag(subchild)
                if subtag == "transition":
                    on_event = subchild.get("on", "")
                    to_state = subchild.get("to", "")
                    if state_id and on_event and to_state:
                        transitions.append((state_id, on_event, to_state))
                if subtag == "secured":
                    attrs = subchild.get("attributes", "")
                    roles = [r.strip().lstrip("ROLE_") for r in attrs.split(",") if r.strip()]
                    guards.append(
                        {
                            "endpoint_id": "",
                            "path": f"/{flow_id}/{state_id}",
                            "method": "GET",
                            "kind": "role_required",
                            "roles": roles,
                            "file": str(file_path),
                            "line": 0,
                        }
                    )

    if not view_states:
        return {}

    webflow_def = {
        "flow_id": flow_id,
        "view_states": view_states,
        "transitions": transitions,
    }

    result: dict[str, list[Any]] = {"webflow_flows": [webflow_def]}
    if guards:
        result["guards"] = guards
    nav_edges = [
        {
            "from_screen": t[0],
            "to_screen": t[2],
            "kind": "link",
            "file": str(file_path),
            "line": 0,
        }
        for t in transitions
    ]
    if nav_edges:
        result["nav_edges"] = nav_edges
    return result
