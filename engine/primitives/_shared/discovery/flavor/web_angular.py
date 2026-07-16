from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class WebAngularFlavor:
    """Flavor pack for Angular single-page applications."""

    name = "web_angular"

    def detect(self, code_root: Path) -> str:
        """Return 'high' if this looks like an Angular project."""
        # angular.json is the canonical Angular workspace marker
        if (code_root / "angular.json").is_file():
            return "high"
        # package.json with @angular/core dependency
        pkg = code_root / "package.json"
        if pkg.is_file():
            text = pkg.read_text(errors="replace")
            if "@angular/core" in text:
                return "high"
        return "none"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:
        from primitives.functional_map import run_sub_extractors  # noqa: PLC0415

        return run_sub_extractors(("angular_routes", "angular_forms"), file_path, content)

    def recommended_section_branches(self) -> list[str]:
        return ["web_angular"]
