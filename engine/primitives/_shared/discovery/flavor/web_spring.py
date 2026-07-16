from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


class WebSpringFlavor:
    def detect(self, code_root: Path) -> Confidence:
        if (code_root / "pom.xml").is_file():
            return "high"
        return "low"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:  # noqa: ARG002
        return {}

    def recommended_section_branches(self) -> list[str]:
        return ["User Flows — Spring MVC mappings"]
