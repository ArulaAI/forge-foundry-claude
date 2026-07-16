from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class WebHybridJspSpaFlavor:
    """Flavor pack for hybrid JSP/Spring MVC + embedded SPA (Angular/React/Vue) apps.

    Typical pattern: server renders JSP pages for most flows, but one or more
    features use an Angular/React/Vue SPA served from a static subfolder.
    Detection requires BOTH a JSP/Spring MVC backend AND a JS SPA frontend.
    """

    name = "web_hybrid_jsp_spa"

    def detect(self, code_root: Path) -> str:
        """Return 'high' if the project mixes JSP/Spring MVC with a SPA frontend."""
        has_jsp = any(code_root.rglob("*.jsp")) or any(code_root.rglob("*.jspx"))
        if not has_jsp:
            return "none"

        # Check for SPA frontend indicators
        pkg_json = code_root / "package.json"
        src_pkg = code_root / "src" / "main" / "webapp" / "package.json"
        frontend_pkg = code_root / "frontend" / "package.json"

        has_spa = False
        for pkg in (pkg_json, src_pkg, frontend_pkg):
            if pkg.is_file():
                text = pkg.read_text(errors="replace")
                if any(fw in text for fw in ('"react"', '"vue"', '"@angular/core"')):
                    has_spa = True
                    break

        # Also check for angular.json in root or subfolder
        if not has_spa:
            for _angular_json in code_root.rglob("angular.json"):
                has_spa = True
                break

        if not has_spa:
            # Check for .tsx/.vue files under src/main/webapp or frontend/
            for subdir in ("frontend", "src/main/webapp"):
                subpath = code_root / subdir
                if subpath.is_dir() and (
                    list(subpath.rglob("*.tsx")) or list(subpath.rglob("*.vue"))
                ):
                    has_spa = True
                    break

        return "high" if (has_jsp and has_spa) else "none"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:
        """Delegate to both Spring MVC and React/Angular/Vue sub-extractors."""
        from primitives.functional_map import run_sub_extractors  # noqa: PLC0415

        return run_sub_extractors(
            (
                "spring_mvc",
                "jsp_forms",
                "react_routes",
                "react_forms",
                "vue_routes",
                "angular_routes",
            ),
            file_path,
            content,
        )

    def recommended_section_branches(self) -> list[str]:
        return ["web_hybrid_jsp_spa"]
