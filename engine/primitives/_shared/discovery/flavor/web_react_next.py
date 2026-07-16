from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


class WebReactNextFlavor:
    def detect(self, code_root: Path) -> Confidence:
        pkg = code_root / "package.json"
        if pkg.is_file():
            text = pkg.read_text(errors="ignore")
            if "next" in text or "react-router" in text:
                return "high"
        return "low"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:  # noqa: ARG002
        return {}

    def recommended_section_branches(self) -> list[str]:
        return ["User Flows — React/Next route table"]
