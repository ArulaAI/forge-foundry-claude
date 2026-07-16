from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_VUE_ROUTER_SIGNALS = (
    "vue-router",
    "createRouter",
    "VueRouter",
    "useRouter",
    "<router-link",
    "router-view",
    "RouterLink",
    "RouterView",
)

# Vue Router route definition: { path: '/foo', component: Bar }
_ROUTE_PATH_RE = re.compile(r"path\s*:\s*['\"]([^'\"]+)['\"]")
# Component assignment in route: component: ComponentName or component: () => import(...)
_ROUTE_COMPONENT_RE = re.compile(
    r"component\s*:\s*(?:(?:\(\s*\)\s*=>\s*import\s*\(['\"]([^'\"]+)['\"])|(\w+))"
)
# Navigation guard: meta: { requiresAuth: true } or meta: { roles: ['ADMIN'] }
_META_AUTH_RE = re.compile(r"requiresAuth\s*:\s*true")
_META_ROLES_RE = re.compile(r"roles\s*:\s*\[([^\]]+)\]")
# router.push / router.replace navigation
_PUSH_RE = re.compile(r"(?:router|this\.\$router)\s*\.\s*push\s*\(\s*['\"]([^'\"]+)['\"]")
# RouterLink / router-link to attribute
_ROUTER_LINK_RE = re.compile(r'(?:<router-link|<RouterLink)\s[^>]*to=["\']([^"\']+)["\']')
# axios/fetch API calls
_API_CALL_RE = re.compile(
    r'(?:axios|fetch)\s*\.\s*(post|put|patch|get|delete)\s*\(\s*["\']([^"\']+)["\']'
)


def parse(file_path: Path, content: str) -> dict[str, list[Any]]:
    """Returns {} if file has no Vue Router content."""
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".js", ".ts", ".vue"):
        return {}
    if not any(sig in content for sig in _VUE_ROUTER_SIGNALS):
        return {}

    endpoints: list[dict[str, Any]] = []
    nav_edges: list[dict[str, Any]] = []
    guards: list[dict[str, Any]] = []

    stem = Path(file_path).stem

    # Extract routes — find path: + component: pairs in the same route block
    # Walk through path matches, then look ahead for nearby component
    for pm in _ROUTE_PATH_RE.finditer(content):
        path = pm.group(1)
        if path in ("*", "/:pathMatch(.*)*", "/:catchAll(.*)"):
            continue
        # Look for component within 200 chars ahead
        window = content[pm.end() : pm.end() + 300]
        cm = _ROUTE_COMPONENT_RE.search(window)
        if cm:
            handler = cm.group(1) or cm.group(2) or stem
            # For lazy imports, use the filename stem
            if "/" in handler:
                handler = handler.split("/")[-1]
        else:
            handler = path.strip("/").replace("/", "_") or "root"
        endpoints.append(
            {
                "path": path,
                "method": "GET",
                "handler": handler,
                "file": str(file_path),
                "line": content[: pm.start()].count("\n") + 1,
            }
        )

        # Check for meta requiresAuth or roles within 400 chars of the path
        meta_window = content[pm.start() : pm.end() + 400]
        if _META_AUTH_RE.search(meta_window):
            rm = _META_ROLES_RE.search(meta_window)
            roles = [r.strip().strip("'\"") for r in rm.group(1).split(",")] if rm else []
            guards.append(
                {
                    "endpoint_id": "",
                    "kind": "requiresAuth",
                    "roles": roles,
                    "file": str(file_path),
                    "line": content[: pm.start()].count("\n") + 1,
                }
            )

    # router-link nav edges
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

    # router.push nav edges
    for m in _PUSH_RE.finditer(content):
        nav_edges.append(
            {
                "from_screen": stem,
                "to_screen": m.group(1),
                "kind": "link",
                "file": str(file_path),
                "line": content[: m.start()].count("\n") + 1,
            }
        )

    # API calls
    for m in _API_CALL_RE.finditer(content):
        endpoints.append(
            {
                "path": m.group(2),
                "method": m.group(1).upper(),
                "handler": stem,
                "file": str(file_path),
                "line": content[: m.start()].count("\n") + 1,
            }
        )

    result: dict[str, list[Any]] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if nav_edges:
        result["nav_edges"] = nav_edges
    if guards:
        result["guards"] = guards
    return result
