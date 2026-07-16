from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_ANGULAR_SIGNALS = (
    "@angular/router",
    "RouterModule",
    "Routes",
    "ActivatedRoute",
    "Router",
    "routerLink",
    "canActivate",
    "loadChildren",
)

# Angular route: { path: 'foo', component: FooComponent }
_ROUTE_PATH_RE = re.compile(r"path\s*:\s*['\"]([^'\"]*)['\"]")
_ROUTE_COMPONENT_RE = re.compile(r"component\s*:\s*(\w+)")
# Lazy load: loadChildren: () => import('./module').then(...)
_LOAD_CHILDREN_RE = re.compile(r"loadChildren\s*:\s*\(\s*\)\s*=>\s*import\s*\(['\"]([^'\"]+)['\"]")
# Auth guard: canActivate: [AuthGuard] or canActivateChild
_CAN_ACTIVATE_RE = re.compile(r"canActivate(?:Child)?\s*:\s*\[([^\]]+)\]")
# Roles from guard data: data: { roles: ['ADMIN'] }
_DATA_ROLES_RE = re.compile(r"roles\s*:\s*\[([^\]]+)\]")
# router.navigate
_NAVIGATE_RE = re.compile(
    r"(?:this\.router|router)\s*\.\s*navigate(?:ByUrl)?\s*\(\s*['\"]([^'\"]+)['\"]"
)
# routerLink directive
_ROUTER_LINK_RE = re.compile(r'routerLink=["\']([^"\']+)["\']')
# HTTP client calls
_HTTP_CALL_RE = re.compile(
    r'(?:this\.http|http)\s*\.\s*(post|put|patch|get|delete)\s*\(\s*["\']([^"\']+)["\']'
)
# HttpClient with template string — skip (not extractable statically)


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file has no Angular Router content."""
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".ts",):
        return {}
    if not any(sig in content for sig in _ANGULAR_SIGNALS):
        return {}

    endpoints: list[dict[str, Any]] = []
    nav_edges: list[dict[str, Any]] = []
    guards: list[dict[str, Any]] = []

    stem = Path(file_path).stem

    # Route definitions
    for pm in _ROUTE_PATH_RE.finditer(content):
        path = pm.group(1)
        if path == "**":
            continue
        window = content[pm.end() : pm.end() + 400]

        # Component handler
        cm = _ROUTE_COMPONENT_RE.search(window)
        lm = _LOAD_CHILDREN_RE.search(window)
        if cm:
            handler = cm.group(1)
        elif lm:
            handler = lm.group(1).split("/")[-1]
        else:
            handler = path.replace("/", "_") or "root"

        endpoints.append(
            {
                "path": "/" + path.lstrip("/") if path else "/",
                "method": "GET",
                "handler": handler,
                "file": str(file_path),
                "line": content[: pm.start()].count("\n") + 1,
            }
        )

        # Guards within the same route object (~400 chars)
        route_window = content[pm.start() : pm.end() + 500]
        cam = _CAN_ACTIVATE_RE.search(route_window)
        if cam:
            guard_names = [g.strip() for g in cam.group(1).split(",")]
            # Try to extract roles from data: { roles: [...] }
            drm = _DATA_ROLES_RE.search(route_window)
            roles = [r.strip().strip("'\"") for r in drm.group(1).split(",")] if drm else []
            guards.append(
                {
                    "endpoint_id": "",
                    "kind": "canActivate",
                    "roles": roles or guard_names,
                    "file": str(file_path),
                    "line": content[: pm.start()].count("\n") + 1,
                }
            )

    # routerLink nav edges
    for m in _ROUTER_LINK_RE.finditer(content):
        nav_edges.append(
            {
                "from_screen": stem,
                "to_screen": m.group(1),
                "kind": "link",
                "file": str(file_path),
                "line": content[: m.start()].count("\n") + 1,
            }
        )

    # router.navigate nav edges
    for m in _NAVIGATE_RE.finditer(content):
        nav_edges.append(
            {
                "from_screen": stem,
                "to_screen": m.group(1),
                "kind": "link",
                "file": str(file_path),
                "line": content[: m.start()].count("\n") + 1,
            }
        )

    # HTTP client API calls
    for m in _HTTP_CALL_RE.finditer(content):
        endpoints.append(
            {
                "path": m.group(2),
                "method": m.group(1).upper(),
                "handler": stem,
                "file": str(file_path),
                "line": content[: m.start()].count("\n") + 1,
            }
        )

    result: dict[str, Any] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if nav_edges:
        result["nav_edges"] = nav_edges
    if guards:
        result["guards"] = guards
    return result
