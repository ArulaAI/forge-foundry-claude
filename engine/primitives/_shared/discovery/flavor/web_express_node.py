from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence

_ROUTE = re.compile(
    r"""\.(?P<method>get|post|put|patch|delete)\(\s*['"](?P<path>[^'"]+)['"](?P<mids>[^)]*)""",
    re.IGNORECASE,
)
_REQUIRE_ROLE = re.compile(r"requireRole\(\s*['\"](?P<role>[^'\"]+)['\"]")
_REQUIRE_AUTH = re.compile(r"\brequireAuth\b")


class WebExpressNodeFlavor:
    """Express/Node route + guard + role extractor for S61."""

    def detect(self, code_root: Path) -> Confidence:
        pkg = code_root / "package.json"
        if pkg.is_file() and "express" in pkg.read_text(errors="ignore"):
            return "high"
        if any(code_root.rglob("*.js")):
            return "low"
        return "low"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:
        endpoints: list[object] = []
        guards: list[object] = []
        roles: list[object] = []
        for m in _ROUTE.finditer(content):
            line = content[: m.start()].count("\n") + 1
            path = m.group("path")
            method = m.group("method").upper()
            mids = m.group("mids") or ""
            endpoints.append(
                {
                    "path": path,
                    "method": method,
                    "handler": _last_ident(mids),
                    "file": str(file_path),
                    "line": line,
                }
            )
            role_m = _REQUIRE_ROLE.search(mids)
            if role_m:
                role = role_m.group("role")
                guards.append(
                    {
                        "path": path,
                        "method": method,
                        "kind": "role_required",
                        "roles": [role],
                        "file": str(file_path),
                        "line": line,
                    }
                )
                roles.append({"name": role, "source": "code"})
            elif _REQUIRE_AUTH.search(mids):
                guards.append(
                    {
                        "path": path,
                        "method": method,
                        "kind": "auth_required",
                        "roles": [],
                        "file": str(file_path),
                        "line": line,
                    }
                )
            else:
                guards.append(
                    {
                        "path": path,
                        "method": method,
                        "kind": "public",
                        "roles": [],
                        "file": str(file_path),
                        "line": line,
                    }
                )
        return {"endpoints": endpoints, "guards": guards, "roles": roles, "nav_edges": []}

    def recommended_section_branches(self) -> list[str]:
        return ["User Flows — Express/Node route graph"]


def _last_ident(mids: str) -> str:
    idents = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", mids)
    return idents[-1] if idents else "handler"
